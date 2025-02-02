import matplotlib as mpl
#import matplotlib.pylab as plt
from astropy.io import fits
import astropy.units as u
from astropy.table import Table
from astropy.stats import LombScargle
import pandas as pd
import numpy as np
#import exoplanet as xo
import os
import celerite
from celerite import terms
from scipy.optimize import minimize, curve_fit
from scipy import stats
from scipy import signal
import time as timing
import traceback

from flareTools import FINDflare, IRLSSpline, id_segments, update_progress, aflare1, autocorr_estimator

mpl.rcParams.update({'font.size': 18, 'font.family': 'STIXGeneral', 'mathtext.fontset': 'stix',
                            'image.cmap': 'viridis'})

def iterGaussProc(time, flux, flux_err, period_guess, interval=1, num_iter=20, debug=True):
    if interval > 1:
        # Start by downsampling the data before doing GP regression
        # Using an interval of 15 takes us from 2 minute to 30 minute cadence
        x = np.empty(len(time)//interval)
        y = np.empty(len(flux)//interval)
        yerr = np.empty(len(flux_err)//interval)

        # Calculate the average of every interval of points
        for idx in range(len(x)):
            i1 = idx*interval
            i2 = (idx+1)*interval
            if i2 > len(time)-1:
                i2 = len(time)-1
            x[idx] = np.mean(time[i1:i2])
            y[idx] = np.mean(flux[i1:i2])
            yerr[idx] = np.mean(flux_err[i1:i2])
    else:
        x = np.asarray(time)
        y = np.asarray(flux)
        yerr = np.asarray(flux_err)
    
    if debug:
        print('Run iterative GP regression with i=' + str(interval) + ' (' + str(len(x)) + ' points)')
    
    # Here is the kernel we will use for the GP regression
    # It consists of a sum of two stochastically driven damped harmonic
    # oscillators. One of the terms has Q fixed at 1/sqrt(2), which
    # forces it to be non-periodic. There is also a white noise term
    # included.
    
    # A non-periodic component
    Q = 1.0 / np.sqrt(2.0)
    w0 = 3.0
    S0 = np.var(y) / (w0 * Q)
    bounds = dict(log_S0=(-20, 15), log_Q=(-15, 15), log_omega0=(-15, 15))
    kernel = terms.SHOTerm(log_S0=np.log(S0), log_Q=np.log(Q), log_omega0=np.log(w0),
                           bounds=bounds)
    kernel.freeze_parameter('log_Q')

    # A periodic component
    Q = 1.0
    w0 = 2*np.pi/period_guess
    S0 = np.var(y) / (w0 * Q)
    kernel += terms.SHOTerm(log_S0=np.log(S0), log_Q=np.log(Q), log_omega0=np.log(w0),
                            bounds=bounds)

    # Now calculate the covariance matrix using the initial
    # kernel parameters
    gp = celerite.GP(kernel, mean=np.mean(y))
    gp.compute(x, yerr)

    def neg_log_like(params, y, gp, m):
        gp.set_parameter_vector(params)
        return -gp.log_likelihood(y[m])

    def grad_neg_log_like(params, y, gp,m ):
        gp.set_parameter_vector(params)
        return -gp.grad_log_likelihood(y[m])[1]

    bounds = gp.get_parameter_bounds()
    initial_params = gp.get_parameter_vector()
    
    if debug:
        print(initial_params)
    
    # Find the best fit kernel parameters. We want to try to ignore the flares
    # when we do the fit. To do this, we will repeatedly find the best fit
    # solution to the kernel model, calculate the covariance matrix, predict
    # the flux and then mask out points based on how far they deviate from
    # the model. After a few passes, this should cause the model to fit mostly
    # to periodic features.
    m = np.ones(len(x), dtype=bool)
    for i in range(num_iter):
        if debug:
            print('Iteration ' + str(i))
        n_pts_prev = np.sum(m)
        gp.compute(x[m], yerr[m])
        soln = minimize(neg_log_like, initial_params, jac=grad_neg_log_like,
                        method='L-BFGS-B', bounds=bounds, args=(y, gp, m))
        gp.set_parameter_vector(soln.x)
        initial_params = soln.x
        mu = gp.predict(y[m], x, return_cov=False, return_var=False)
        var = np.nanvar(y - mu)
        sig = np.sqrt(var)

        m0 = y - mu < sig
        m[m==1] = m0[m==1]
        n_pts = np.sum(m)
        print(n_pts_prev, n_pts)
        if n_pts <= 1000:
            raise ValueError('GP iteration threw out too many points')
            break
        if (n_pts == n_pts_prev):
            break

    gp.compute(x[m], yerr[m])
    mu = gp.predict(y[m], time, return_cov=False, return_var=False)
    
    return mu, gp.get_parameter_vector()

def gaussian(x, mu, sigma, A):
    return A/np.sqrt(2*np.pi*sigma**2)*np.exp(-(x - mu)**2/sigma**2/2)

def redChiSq(y_model, ydata, yerr, dof):
    chi2 = np.sum((ydata - y_model)**2/yerr**2)/dof
    return chi2

def vetFlare(x, y, yerr, tstart, tstop, dx_fac=5):
    '''
    Given a flare detection, try to fit a gaussian and a flare model from
    Davenport 2014 to the light curve segment. If the reduced chi squared
    for the gaussian is smaller, this is likely not a flare.
    Parameters
    ----------
    x : numpy array
        time values from the entire light curve
    y : numpy array
        flux values from the entire light curve
    yerr : numpy array
        error in the flux values
    tstart : float
        Start time of the flare detection
    tstop : float
        End time of the flare detection
    dx_fac : float, optional
        Factor by which to expand the flare window when fitting a model
    Returns
    -------
        popt1 - Best fit parameters for the gaussian model
        pstd1 - Error on the gaussian best fit parameters
        chi1 - Reduced chi squared of gaussian fit
        popt2 - Best fit parameters for the flare model
        pstd2 - Error on the flare best fit parameters
        chi2 - Reduced chi squared of flare fit
    '''
    # Use a segment of the light curve that is dx_fac times the width of the flare detection
    dx = tstop - tstart
    x1 = tstart - dx*dx_fac/2
    x2 = tstop + dx*dx_fac/2
    mask = (x > x1) & (x < x2)
    
    mu0 = (tstart + tstop)/2
    sig0 = (tstop - tstart)/2
    A0 = 1
    skew = 0

    try:
        # Get the skew by treating time = x and flux = p(x). Calculate the
        # third moment of p(x)
        A = 1/np.trapz(y[mask], x[mask])
        mu = np.trapz(x[mask]*A*y[mask], x[mask])
        var = np.trapz((x[mask] - mu)**2*A*y[mask], x[mask])
        stddev = np.sqrt(np.fabs(var))
        skew = np.trapz((x[mask] - mu)**3*A*y[mask], x[mask])/stddev**3

        # Fit a gaussian to the segment
        popt1, pcov1 = curve_fit(gaussian, x[mask], y[mask], p0=(mu0, sig0, A0), sigma=yerr[mask])
        y_model = gaussian(x[mask], popt1[0], popt1[1], popt1[2])
        chi1 = redChiSq(y_model, y[mask], yerr[mask], len(y[mask]) - 3)
    
        # Fit the Davenport 2014 flare model to the segment
        popt2, pcov2 = curve_fit(aflare1, x[mask], y[mask], p0=(mu0, sig0, A0), sigma=yerr[mask])
        y_model = aflare1(x[mask], popt2[0], popt2[1], popt2[2])
        chi2 = redChiSq(y_model, y[mask], yerr[mask], len(y[mask]) - 3)
    except:
        empty = np.zeros(3)
        return empty, empty, -1, empty, empty, -1, 0, 0

    n_pts = len(x[mask])
    n_pts_true = np.floor(((x2-x1)*u.d).to(u.min).value/2)
    coverage = n_pts/n_pts_true

    return popt1, np.sqrt(pcov1.diagonal()), chi1, popt2, np.sqrt(pcov2.diagonal()), chi2, skew, coverage

def measure_ED(x, y, yerr, tpeak, fwhm, num_fwhm=10):
    '''
    Measure the equivalent duration of a flare in a smoothed light
    curve. FINDflare typically doesnt identify the entire flare, so
    integrate num_fwhm/2 away from the peak. As long as the light curve
    is flat away from the flare, the region around the flare should
    not significantly contribute.
    Parameters
    ----------
    x : numpy array
        time values from the entire light curve
    y : numpy array
        flux values from the entire light curve
    yerr : numpy array
        error in the flux values
    tpeak : float
        Peak time of the flare detection
    fwhm : float
        Full-width half maximum of the flare
    num_fwhm : float, optional
        Size of the integration window in units of fwhm
    Returns
    -------
        ED - Equivalent duration of the flare
        ED_err - The uncertainty in the equivalent duration
    '''
    print(tpeak, fwhm)
    try:
        width = fwhm*num_fwhm
        istart = np.argwhere(x > tpeak - width/2)[0]
        ipeak = np.argwhere(x > tpeak)[0]
        istop = np.argwhere(x > tpeak + width/2)[0]
    
        dx = np.diff(x)
        x = x[:-1]
        y = y[:-1]
        yerr = yerr[:-1]
        mask = (x > x[istart]) & (x < x[istop])
        ED = np.trapz(y[mask], x[mask])
        #ED_err = np.sqrt(np.trapz(yerr[mask], x[mask])**2)
        ED_err = np.sqrt(np.sum((dx[mask]*yerr[mask])**2))

    except IndexError:
        return -1, -1
    
    return ED, ED_err

def procFlaresGP(files, sector, cpa_param, makefig=True, clobberPlots=False, clobberGP=False, writeLog=False, writeDFinterval=1, debug=False, gpInterval=15):
 
    # Columns for flare table
    FL_files = np.array([])
    FL_TICs = np.array([])
    FL_id = np.array([])
    FL_t0 = np.array([])
    FL_t1 = np.array([])
    FL_f0 = np.array([])
    FL_f1 = np.array([])
    FL_smo_pk = np.array([])
    FL_smo_sig = np.array([])
    FL_ed = np.array([])
    FL_ed_err = np.array([])
    FL_skew = np.array([])
    FL_cover = np.array([])
    FL_mu = np.array([])
    FL_std = np.array([])
    FL_g_amp = np.array([])
    FL_mu_err = np.array([])
    FL_std_err = np.array([])
    FL_g_amp_err = np.array([])
    FL_tpeak = np.array([])
    FL_fwhm = np.array([])
    FL_f_amp = np.array([])
    FL_tpeak_err = np.array([])
    FL_fwhm_err = np.array([])
    FL_f_amp_err = np.array([])
    FL_g_chisq = np.array([])
    FL_f_chisq = np.array([])
    
    # Columns for param table
    P_median = np.array([])
    P_s_window = np.array([])
    P_acf_1dt = np.array([])
    P_ls_per = np.array([])
    P_p_res = np.array([])
    P_gp_log_s00 = np.array([])
    P_gp_log_omega00 = np.array([])
    P_gp_log_s01 = np.array([])
    P_gp_log_omega01 = np.array([])
    P_gp_log_q1 = np.array([])
    
    failed_files = []
    
    if writeLog:
        if os.path.exists(sector + '.log'):
            os.remove(sector + '.log')

    for k in range(len(files)):
        print(k)
        start_time = timing.time()
        filename = files[k].split('/')[-1]
        TIC = int(filename.split('-')[-3])
        print(TIC)
        
        if debug:
            print('Open ' + files[k])

        gp_data_file = files[k] + '.gp'
        gp_param_file = files[k] + '.gp.par'
        median = -1
        s_window = -1
        acf_1dt = -1
        ls_per = -1
        p_signal = -1
        gp_log_s00 = -1
        gp_log_omega00 = -1
        gp_log_s01 = -1
        gp_log_omega01 = -1
        gp_log_q1 = -1

        with fits.open(files[k], mode='readonly') as hdulist:
            try:
                tess_bjd = hdulist[1].data['TIME']
                quality = hdulist[1].data['QUALITY']
                pdcsap_flux = hdulist[1].data['PDCSAP_FLUX']
                pdcsap_flux_error = hdulist[1].data['PDCSAP_FLUX_ERR']
            except:
                P_median = np.append(P_median, median)
                P_s_window = np.append(P_s_window, s_window)
                P_acf_1dt = np.append(P_acf_1dt, acf_1dt)
                P_ls_per = np.append(P_ls_per, ls_per)
                P_p_res = np.append(P_p_res, p_signal)
                P_gp_log_s00 = np.append(P_gp_log_s00, gp_log_s00)
                P_gp_log_omega00 = np.append(P_gp_log_omega00, gp_log_omega00)
                P_gp_log_s01 = np.append(P_gp_log_s01, gp_log_s01)
                P_gp_log_omega01 = np.append(P_gp_log_omega01, gp_log_omega01)
                P_gp_log_q1 = np.append(P_gp_log_q1, gp_log_q1)
                print(files[k].split('/')[-1] + ' failed during reading')
                failed_files.append(files[k].split('/')[-1])
                np.savetxt(gp_data_file, ([]))
                continue
            
        # There were a few cases where NaN values had quality = 0
        ok_cut = (quality == 0) & (~np.isnan(tess_bjd)) & (~np.isnan(pdcsap_flux)) & (~np.isnan(pdcsap_flux_error))
            
        if debug:
            print('Find segments')
        
        # Split data into segments, but put it all back together before doing GP regression
        # We really just want to trim the edges of the segments here
        dt_limit = 12/24 # 12 hours
        trim = 12/24 # 4 hours
        istart, istop = id_segments(tess_bjd[ok_cut], dt_limit, dt_trim=trim)
        
        tess_bjd_trim = np.array([])
        pdcsap_flux_trim = np.array([])
        pdcsap_flux_error_trim = np.array([])
        
        for seg_idx in range(len(istart)):
            tess_bjd_seg = tess_bjd[ok_cut][istart[seg_idx]:istop[seg_idx]]
            pdcsap_flux_seg = pdcsap_flux[ok_cut][istart[seg_idx]:istop[seg_idx]]
            pdcsap_flux_error_seg = pdcsap_flux_error[ok_cut][istart[seg_idx]:istop[seg_idx]]
            
            tess_bjd_trim = np.concatenate((tess_bjd_trim, tess_bjd_seg), axis=0)
            pdcsap_flux_trim = np.concatenate((pdcsap_flux_trim, pdcsap_flux_seg), axis=0)
            pdcsap_flux_error_trim = np.concatenate((pdcsap_flux_error_trim, pdcsap_flux_error_seg), axis=0)
            
        # Mask out eclipses and transits
        # Leave this disabled for now, introduces problems with bottoms of starspot oscillations
        # being marked as eclipses. Might be easier to just throw eclpises out in the flare table
        # at the end
        """istart_e, istop_e = EasyE(pdcsap_flux_trim, pdcsap_flux_error_trim)
        mask = np.ones(len(time), dtype=bool)
        for idx in range(len(istart_e)):
            if debug:
                print('Mask out eclipse at t='+str(tess_bjd_trim[istart_e[idx]]))
            mask[istart_e[idx]:istop_e[idx]] = 0
        tess_bjd_trim = tess_bjd_trim[mask]
        pdcsap_flux_trim = pdcsap_flux_trim[mask]
        pdcsap_flux_error_trim = pdcsap_flux_error_trim[mask]"""
            
        tbl = Table([tess_bjd_trim, pdcsap_flux_trim, pdcsap_flux_error_trim], 
                     names=('TIME', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR'))
        df_tbl = tbl.to_pandas()

        median = np.nanmedian(df_tbl['PDCSAP_FLUX'])
        
        if debug:
            print('Estimate periods')
        
        acf = autocorr_estimator(tbl['TIME'], tbl['PDCSAP_FLUX']/median,
                                    yerr=tbl['PDCSAP_FLUX_ERR']/median,
                                    min_period=0.1, max_period=27, max_peaks=2)

        if len(acf['peaks']) > 0:
            acf_1dt = acf['peaks'][0]['period']
            mask = np.where((acf['autocorr'][0] == acf['peaks'][0]['period']))[0]
            acf_1pk = acf['autocorr'][1][mask][0]
            s_window = int(acf_1dt/np.fabs(np.nanmedian(np.diff(df_tbl['TIME']))) / 6)
        else:
            acf_1dt = (tbl['TIME'][-1] - tbl['TIME'][0])/2
            s_window = 128
            
        freq = np.linspace(1e-2, 100.0, 10000)
        model = LombScargle(tbl['TIME'], tbl['PDCSAP_FLUX']/median)
        power = model.power(freq, method='fast', normalization='psd')
        power /= len(tbl['TIME'])
        ls_per = 1.0 / freq[np.argmax(power)]
            
        P_median = np.append(P_median, median)
        P_s_window = np.append(P_s_window, s_window)
        P_acf_1dt = np.append(P_acf_1dt, acf_1dt)
        P_ls_per = np.append(P_ls_per, ls_per)
        
        if debug:
            print('GP smoothing')
            
        if os.path.exists(gp_data_file) and not clobberGP:
            if debug:
                print('GP file exists, loading')
                
            # Failed GP regression will produce an empty file
            if os.path.getsize(gp_data_file) == 0:
                print(files[k].split('/')[-1] + ' failed (previously) during GP regression')
                P_p_res = np.append(P_p_res, p_signal)
                P_gp_log_s00 = np.append(P_gp_log_s00, gp_log_s00)
                P_gp_log_omega00 = np.append(P_gp_log_omega00, gp_log_omega00)
                P_gp_log_s01 = np.append(P_gp_log_s01, gp_log_s01)
                P_gp_log_omega01 = np.append(P_gp_log_omega01, gp_log_omega01)
                P_gp_log_q1 = np.append(P_gp_log_q1, gp_log_q1)
                failed_files.append(files[k].split('/')[-1])
                continue
                
            time, smo = np.loadtxt(gp_data_file)
        else:
            smo = np.zeros(len(df_tbl['TIME']))
            try:
                if debug:
                    print('No GP file found, running GP regression')
                smo, params = iterGaussProc(df_tbl['TIME'], df_tbl['PDCSAP_FLUX']/median,
                                         df_tbl['PDCSAP_FLUX_ERR']/median, acf_1dt, interval=gpInterval, debug=debug)

                gp_log_s00 = params[0]
                gp_log_omega00 = params[1]
                gp_log_s01 = params[2]
                gp_log_omega01 = params[3]
                gp_log_q1 = params[4]

                freq = np.linspace(1e-2, 100.0, 10000)
                x = df_tbl['TIME']
                y = df_tbl['PDCSAP_FLUX']/median - smo
                model = LombScargle(x, y)
                power = model.power(freq, method='fast', normalization='psd')
                power /= len(x)
                period = 1.0 / freq[np.argmax(power)]
                p_signal = np.max(power)/np.median(power)
                
                if debug:
                    print('GP regression finished, saving results to file')
                
                np.savetxt(gp_param_file, params)
                np.savetxt(gp_data_file, (x, smo))
                

            # If GP regression fails, skip over this light curve and list it at the
            # end of the log file. Write out an empty .gp file
            except:
                traceback.print_exc()
                P_p_res = np.append(P_p_res, p_signal)
                P_gp_log_s00 = np.append(P_gp_log_s00, gp_log_s00)
                P_gp_log_omega00 = np.append(P_gp_log_omega00, gp_log_omega00)
                P_gp_log_s01 = np.append(P_gp_log_s01, gp_log_s01)
                P_gp_log_omega01 = np.append(P_gp_log_omega01, gp_log_omega01)
                P_gp_log_q1 = np.append(P_gp_log_q1, gp_log_q1)
                print(files[k].split('/')[-1] + ' failed during GP regression')
                failed_files.append(files[k].split('/')[-1])
                np.savetxt(gp_data_file, ([]))
                continue
 
        P_p_res = np.append(P_p_res, p_signal)
        P_gp_log_s00 = np.append(P_gp_log_s00, gp_log_s00)
        P_gp_log_omega00 = np.append(P_gp_log_omega00, gp_log_omega00)
        P_gp_log_s01 = np.append(P_gp_log_s01, gp_log_s01)
        P_gp_log_omega01 = np.append(P_gp_log_omega01, gp_log_omega01)
        P_gp_log_q1 = np.append(P_gp_log_q1, gp_log_q1)
        
        if makefig:
            fig, axes = plt.subplots(figsize=(8,8))
            axes.errorbar(df_tbl['TIME'], df_tbl['PDCSAP_FLUX']/median,
                          yerr=df_tbl['PDCSAP_FLUX_ERR']/median, 
                          linestyle=None, alpha=0.15, label='PDCSAP_FLUX')

        if np.sum(ok_cut) < 1000:
            print('Warning: ' + f + ' contains < 1000 good points')

        # Remove flux jumps by masking out sections of the LC where the GP changes
        # suddenly. Take the rolling STD of the GP, smooth it with a gaussian, and
        # cut out sections where this quantity exceeds 1 sigma. This shouldn't mask
        # out flares because the GP should have smoothed over those

        # 30 minute window (15 data points)
        roll = pd.DataFrame(smo).rolling(15, center=True).std().values
        roll = roll.reshape(1, -1)[0]
        mask = np.isfinite(roll)
        time = df_tbl['TIME'][mask]
        flux = df_tbl['PDCSAP_FLUX'][mask]
        error = df_tbl['PDCSAP_FLUX_ERR'][mask]
        smo = smo[mask]
        print(s_window)
        w = signal.gaussian(s_window, s_window)
        y = np.convolve(w/w.sum(), roll[mask],mode='same')
        mask1 = y < np.nanstd(roll[mask])

        time = time[mask1]
        flux = flux[mask1]
        error = error[mask1]
        smo = smo[mask1]
 
        print('Find flares')
        x = np.array(time)
        y = np.array(flux/median - smo)
        yerr =  np.array(error/median)
        
        # Search for flares in the smoothed light curve using change point analysis
        FL = FINDflare(y, yerr,
                        avg_std=True, std_window=s_window, N1=cpa_param[0], N2=cpa_param[1], N3=cpa_param[2])

        for j in range(len(FL[0])):
            if debug:
                print('Found a flare, fitting model to it')
            
            # Try to fit a flare model to the detection
            # Mask out other flare detections when fitting models
            other_mask = np.ones(len(time), dtype=bool)
            for i in range(len(FL[0])):
                s1other, s2other = FL[0][i], FL[1][i]+1
                if i == j:
                    continue
                other_mask[s1other:s2other] = 0
            tstart = time.values[FL[0][j]]
            tstop = time.values[FL[1][j] + 1]
            popt1, pstd1, g_chisq, popt2, pstd2, f_chisq, skew, cover = \
                vetFlare(x[other_mask], y[other_mask], yerr[other_mask], tstart, tstop)
            print(g_chisq, f_chisq, skew, cover)
            
            mu, std, g_amp = popt1[0], popt1[1], popt1[2]
            mu_err, std_err, g_amp_err = pstd1[0], pstd1[1], pstd1[2]
                
            tpeak, fwhm, f_amp = popt2[0], popt2[1], popt2[2]
            tpeak_err, fwhm_err, f_amp_err = pstd2[0], pstd2[1], pstd2[2]
            
            if debug:
                print('Flare model fit, measuring ED')

            mask1 = (x >= tstart) & (x <= tstop)
            fl_median = np.nanmedian(smo[mask1])

            # Now that we have a flare model, measure the equivalent duration
            ED, ED_err = measure_ED(x, y, yerr, tpeak, fwhm)
            
            FL_files = np.append(FL_files, filename)
            FL_TICs = np.append(FL_TICs, TIC)
            FL_id = np.append(FL_id, k)
            FL_t0 = np.append(FL_t0, tstart)
            FL_t1 = np.append(FL_t1, tstop)
            FL_f0 = np.append(FL_f0, median)
            s1, s2 = FL[0][j], FL[1][j]+1
            FL_f1 = np.append(FL_f1, np.nanmax(flux[s1:s2]))
            FL_smo_pk = np.append(FL_smo_pk, np.nanmax(y[mask1]))
            FL_smo_sig = np.append(FL_smo_sig, np.nanstd(y[mask1]))
            FL_ed = np.append(FL_ed, ED)
            FL_ed_err = np.append(FL_ed_err, ED_err)
            
            FL_skew = np.append(FL_skew, skew)
            FL_cover = np.append(FL_cover, cover)
            FL_mu = np.append(FL_mu, mu)
            FL_std = np.append(FL_std, std)
            FL_g_amp = np.append(FL_g_amp, g_amp)
            FL_mu_err = np.append(FL_mu_err, mu_err)
            FL_std_err = np.append(FL_std_err, std_err)
            FL_g_amp_err = np.append(FL_g_amp_err, g_amp_err)
            
            FL_tpeak = np.append(FL_tpeak, tpeak)
            FL_fwhm = np.append(FL_fwhm, fwhm)
            FL_f_amp = np.append(FL_f_amp, f_amp)
            FL_tpeak_err = np.append(FL_tpeak_err, tpeak_err)
            FL_fwhm_err = np.append(FL_fwhm_err, fwhm_err)
            FL_f_amp_err = np.append(FL_f_amp_err, f_amp_err)
            
            FL_g_chisq = np.append(FL_g_chisq, g_chisq)
            FL_f_chisq = np.append(FL_f_chisq, f_chisq)

            if makefig:
                axes.scatter(df_tbl['TIME'][s1:s2],
                             df_tbl['PDCSAP_FLUX'][s1:s2]/median,
                             color='r', label='_nolegend_')
                axes.scatter([],[], color='r', label='Flare?')
                
        if makefig: 
            axes.set_xlabel('Time [BJD - 2457000, days]')
            axes.set_ylabel('Normalized Flux')
            axes.legend()
            axes.set_title(filename)

            figname = files[k] + '.jpeg'
            makefig = ((not os.path.exists(figname)) | clobberPlots)
            plt.savefig(figname, bbox_inches='tight', pad_inches=0.25, dpi=100)
            plt.close()
        
        if writeLog:
            if debug:
                print('Write to logfile')
            
            with open(sector+'.log', 'a') as f:
                time_elapsed = timing.time() - start_time
                num_flares = len(FL[0])
                
                f.write('{:^15}'.format(str(k+1) + '/' + str(len(files))) + \
                        '{:<60}'.format(files[k].split('/')[-1]) + '{:<20}'.format(time_elapsed) + '{:<10}'.format(num_flares) + '\n')
                
        
        # Periodically write to the flare table file and param table file
        l = k+1
        ALL_TIC = pd.Series(files).str.split('-', expand=True).iloc[:,-3].astype('int')
        ALL_FILES = pd.Series(files).str.split('/', expand=True).iloc[:,-1]
        flare_out = pd.DataFrame(data={'file':FL_files,'TIC':FL_TICs,
                               't0':FL_t0, 't1':FL_t1,
                               'med':FL_f0, 'peak':FL_f1, 'smo_pk':FL_smo_pk, 'smo_sig':FL_smo_sig, 'ed':FL_ed,
                               'ed_err':FL_ed_err, 'skew':FL_skew, 'cover':FL_cover, 'mu':FL_mu, 'std':FL_std, 'g_amp': FL_g_amp,
                               'mu_err':FL_mu_err, 'std_err':FL_std_err, 'g_amp_err':FL_g_amp_err,
                               'tpeak':FL_tpeak, 'fwhm':FL_fwhm, 'f_amp':FL_f_amp,
                               'tpeak_err':FL_tpeak_err, 'fwhm_err':FL_fwhm_err,
                               'f_amp_err':FL_f_amp_err,'f_chisq':FL_f_chisq, 'g_chisq':FL_g_chisq})
        flare_out.to_csv(sector+ '_flare_out.csv', index=False)
        if debug:
            print('Write to flare table (' +str(len(flare_out)) + ' lines)')
            
        param_out = pd.DataFrame(data={'file':ALL_FILES[:l],'TIC':ALL_TIC[:l], 'med':P_median[:l], 's_window':P_s_window[:l], 'acf_1dt':P_acf_1dt[:l],
                                       'ls_per':P_ls_per[:l], 'p_res':P_p_res[:l]})
        param_out.to_csv(sector+ '_param_out.csv', index=False)
        
    print(str(len(ALL_TIC[FL_id])) + ' flares found across ' + str(len(files)) + ' files')
    print(str(len(failed_files)) + ' light curves failed')
    if writeLog:
        with open(sector+'.log', 'a') as f:
            f.write('\n')
            for fname in failed_files:
                f.write(fname + ' failed during GP regression\n')
