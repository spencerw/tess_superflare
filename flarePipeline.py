import matplotlib as mpl
import matplotlib.pyplot as plt
from astropy.io import fits
from astropy.table import Table
import pandas as pd
import numpy as np
import exoplanet as xo
import os
import celerite
from celerite import terms
from scipy.optimize import minimize
import time

from flareTools import FINDflare, IRLSSpline, id_segments, update_progress

mpl.rcParams.update({'font.size': 18, 'font.family': 'STIXGeneral', 'mathtext.fontset': 'stix',
                            'image.cmap': 'viridis'})

def iterGaussProc(time, flux, flux_err, period_guess, interval=50, num_iter=5):
    
    x = time[::interval]
    y = flux[::interval]
    yerr = flux_err[::interval]
    
    # A non-periodic component
    Q = 1.0 / np.sqrt(2.0)
    w0 = 3.0
    S0 = np.var(y) / (w0 * Q)
    bounds = dict(log_S0=(-20, 15), log_Q=(-15, 15), log_omega0=(-15, 15))
    kernel = terms.SHOTerm(log_S0=np.log(S0), log_Q=np.log(Q), log_omega0=np.log(w0),
                           bounds=bounds)
    kernel.freeze_parameter("log_Q")  # We don't want to fit for "Q" in this term

    # A periodic component
    Q = 1.0
    w0 = 2*np.pi/period_guess
    S0 = np.var(y) / (w0 * Q)
    kernel += terms.SHOTerm(log_S0=np.log(S0), log_Q=np.log(Q), log_omega0=np.log(w0),
                            bounds=bounds)
    
    kernel += terms.JitterTerm(log_sigma=np.log(np.median(yerr)),
                               bounds=[(-20.0, 5.0)])

    gp = celerite.GP(kernel, mean=np.mean(y))
    gp.compute(x, yerr)  # You always need to call compute once.

    def neg_log_like(params, y, gp):
        gp.set_parameter_vector(params)
        return -gp.log_likelihood(y)

    def grad_neg_log_like(params, y, gp):
        gp.set_parameter_vector(params)
        return -gp.grad_log_likelihood(y)[1]

    bounds = gp.get_parameter_bounds()
    initial_params = gp.get_parameter_vector()
    pen = 100 # penalty factor to apply to outliers
    yerr_rw = np.copy(yerr)
    for i in range(num_iter):
        gp.compute(x, yerr_rw)
        soln = minimize(neg_log_like, initial_params, jac=grad_neg_log_like,
                        method="L-BFGS-B", bounds=bounds, args=(y, gp))
        gp.set_parameter_vector(soln.x)
        initial_params = soln.x
        mu, var = gp.predict(y, x, return_var=True)
        sig = np.sqrt(var + yerr**2)

        chisq = (y - mu)**2/yerr**2
        yerr_rw = 1/np.sqrt(pen/(yerr**2*(chisq + pen)))
    
    fit_x, fit_y, fit_yerr = x, y, yerr

    gp.compute(fit_x, fit_yerr)
    gp.log_likelihood(fit_y)   
    gp.get_parameter_dict()
    
    # We want mu and var to be the same shape as the time array, need to interpolate
    # since we downsampled
    mu_interp = np.interp(time, time[::interval], mu)
    var_interp = np.interp(time, time[::interval], var)
    
    return mu_interp, var_interp

def procFlaresGP(files, sector, makefig=True, clobberPlots=False, clobberGP=False, writeLog=False):
 
    FL_id = np.array([])
    FL_t0 = np.array([])
    FL_t1 = np.array([])
    FL_f0 = np.array([])
    FL_f1 = np.array([])
    FL_ed = np.array([])
    
    failed_files = []
    
    if writeLog:
        if os.path.exists(sector + '.log'):
            os.remove(sector + '.log')
            with open(sector+'.log', 'a') as f:
                f.write('{:^10}'.format('') + '{:60}'.format('filename') + '{:>10}'.format('time (s)') + '\n')

    for k in range(len(files)):
        start_time = time.time()
        filename = files[k].split('/')[-1]

        with fits.open(files[k], mode='readonly') as hdulist:
            tess_bjd = hdulist[1].data['TIME']
            quality = hdulist[1].data['QUALITY']
            pdcsap_flux = hdulist[1].data['PDCSAP_FLUX']
            pdcsap_flux_error = hdulist[1].data['PDCSAP_FLUX_ERR']
            
        # Split data into segments, but put it all back together before doing GP regression
        # We really just want to trim the edges of the segments here
        dt_limit = 12/24 # 12 hours
        trim = 4/24 # 4 hours
        istart, istop = id_segments(tess_bjd, dt_limit, dt_trim=trim)
        
        tess_bjd_trim = np.array([])
        quality_trim = np.array([])
        pdcsap_flux_trim = np.array([])
        pdcsap_flux_error_trim = np.array([])
        
        for seg_idx in range(len(istart)):
            tess_bjd_seg = tess_bjd[istart[seg_idx]:istop[seg_idx]]
            quality_seg = quality[istart[seg_idx]:istop[seg_idx]]
            pdcsap_flux_seg = pdcsap_flux[istart[seg_idx]:istop[seg_idx]]
            pdcsap_flux_error_seg = pdcsap_flux_error[istart[seg_idx]:istop[seg_idx]]
            
            tess_bjd_trim = np.concatenate((tess_bjd_trim, tess_bjd_seg), axis=0)
            quality_trim = np.concatenate((quality_trim, quality_seg), axis=0)
            pdcsap_flux_trim = np.concatenate((pdcsap_flux_trim, pdcsap_flux_seg), axis=0)
            pdcsap_flux_error_trim = np.concatenate((pdcsap_flux_error_trim, pdcsap_flux_error_seg), axis=0)
            
        ok_cut = quality_trim == 0
        tbl = Table([tess_bjd_trim[ok_cut], pdcsap_flux_trim[ok_cut], pdcsap_flux_error_trim[ok_cut]], 
                     names=('TIME', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR'))
        df_tbl = tbl.to_pandas()

        median = np.nanmedian(df_tbl['PDCSAP_FLUX'])

        acf = xo.autocorr_estimator(tbl['TIME'], tbl['PDCSAP_FLUX']/median,
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
            
        # GP smoothing takes a long time, save mu and var to an ascii file
        gp_file = files[k] + '.gp'
        if os.path.exists(gp_file) and  not clobberGP:
            smo, var = np.loadtxt(gp_file)
        else:
            try:
                smo, var = iterGaussProc(df_tbl['TIME'], df_tbl['PDCSAP_FLUX']/median,
                                         df_tbl['PDCSAP_FLUX_ERR']/median, acf_1dt)
                np.savetxt(gp_file, (smo, var))
            # If GP regression fails, skip over this light curve and list it at the
            # end of the log file
            except celerite.solver.LinAlgError:
                print(files[k].split('/')[-1] + ' failed during GP regression')
                failed_files.append(files[k].split('/')[-1])
                continue
        
        if makefig:
            fig, axes = plt.subplots(figsize=(8,8))
            axes.errorbar(df_tbl['TIME'], df_tbl['PDCSAP_FLUX']/median,
                          yerr=df_tbl['PDCSAP_FLUX_ERR']/median, 
                          linestyle=None, alpha=0.15, label='PDCSAP_FLUX')

        if np.sum(ok_cut) < 1000:
            print('Warning: ' + f + ' contains < 1000 good points')

        sok_cut = np.isfinite(smo)

        FL = FINDflare((df_tbl['PDCSAP_FLUX'][sok_cut] - smo[sok_cut])/median, 
                        df_tbl['PDCSAP_FLUX_ERR'][sok_cut]/median,
                        avg_std=False, N1=4, N2=2, N3=5)

        for j in range(len(FL[0])):
            FL_id = np.append(FL_id, k)
            FL_t0 = np.append(FL_t0, df_tbl['TIME'][sok_cut].values[FL[0][j]])
            FL_t1 = np.append(FL_t1, df_tbl['TIME'][sok_cut].values[FL[1][j]])
            FL_f0 = np.append(FL_f0, median)
            s1, s2 = FL[0][j], FL[1][j]+1
            FL_f1 = np.append(FL_f1, np.nanmax(df_tbl['PDCSAP_FLUX'][sok_cut][s1:s2]))
            ed_val = np.trapz(df_tbl['PDCSAP_FLUX'][sok_cut][s1:s2],
                              df_tbl['TIME'][sok_cut][s1:s2])
            FL_ed = np.append(FL_ed, ed_val)

            if makefig:
                axes.scatter(df_tbl['TIME'][sok_cut][s1:s2],
                             df_tbl['PDCSAP_FLUX'][sok_cut][s1:s2]/median,
                             color='r', label='_nolegend_')
                axes.scatter([],[], color='r', label='Flare?')
                
        if makefig: 
            axes.set_xlabel('Time [BJD - 2457000, days]')
            axes.set_ylabel('Normalized Flux')
            axes.legend()
            axes.set_title(filename)

            figname = filename + '.jpeg'
            makefig = ((not os.path.exists(figname)) | clobberPlots)
            plt.savefig(figname, bbox_inches='tight', pad_inches=0.25, dpi=100)
            plt.close()      
        
        if writeLog:
            with open(sector+'.log', 'a') as f:
                time_elapsed = time.time() - start_time
                
                f.write('{:^10}'.format(str(k+1) + '/' + str(len(files))) + \
                        '{:60}'.format(files[k].split('/')[-1]) + '{:>10}'.format(time_elapsed) + '\n')
        
    ALL_TIC = pd.Series(files).str.split('-', expand=True).iloc[:,-3].astype('int')
    print(str(len(ALL_TIC[FL_id])) + ' flares found across ' + str(len(files)) + ' files')
    print(str(len(failed_files)) + ' light curves failed')
    if writeLog:
        with open(sector+'.log', 'a') as f:
            f.write('\n')
            for fname in failed_files:
                f.write(fname + ' failed during GP regression\n')
                
    flare_out = pd.DataFrame(data={'TIC':ALL_TIC[FL_id],
                                   't0':FL_t0, 't1':FL_t1,
                                   'med':FL_f0, 'peak':FL_f1, 'ed':FL_ed})
    flare_out.to_csv(sector+ '_flare_out.csv')


def procFlares(files, sector, makefig=True, clobber=False, smoothType='roll_med', progCounter=False):

    FL_id = np.array([])
    FL_t0 = np.array([])
    FL_t1 = np.array([])
    FL_f0 = np.array([])
    FL_f1 = np.array([])
    FL_ed = np.array([])

    for k in range(len(files)):
        filename = files[k].split('/')[-1]

        with fits.open(files[k], mode='readonly') as hdulist:
            tess_bjd = hdulist[1].data['TIME']
            quality = hdulist[1].data['QUALITY']
            pdcsap_flux = hdulist[1].data['PDCSAP_FLUX']
            pdcsap_flux_error = hdulist[1].data['PDCSAP_FLUX_ERR']
            
        # Split data into segments
        dt_limit = 12/24 # 12 hours
        trim = 4/24 # 4 hours
        istart, istop = id_segments(tess_bjd, dt_limit, dt_trim=trim)
        for seg_idx in range(len(istart)):
            tess_bjd_seg = tess_bjd[istart[seg_idx]:istop[seg_idx]]
            quality_seg = quality[istart[seg_idx]:istop[seg_idx]]
            pdcsap_flux_seg = pdcsap_flux[istart[seg_idx]:istop[seg_idx]]
            pdcsap_flux_error_seg = pdcsap_flux_error[istart[seg_idx]:istop[seg_idx]]
            
            ok_cut = quality_seg == 0
            tbl = Table([tess_bjd_seg[ok_cut], pdcsap_flux_seg[ok_cut], pdcsap_flux_error_seg[ok_cut]], 
                         names=('TIME', 'PDCSAP_FLUX', 'PDCSAP_FLUX_ERR'))
            df_tbl = tbl.to_pandas()

            median = np.nanmedian(df_tbl['PDCSAP_FLUX'])

            if smoothType == 'spline':
                smo = IRLSSpline(df_tbl['TIME'].values, df_tbl['PDCSAP_FLUX'].values/median,
                                 df_tbl['PDCSAP_FLUX_ERR'].values/median)
            else:
                acf = xo.autocorr_estimator(tbl['TIME'], tbl['PDCSAP_FLUX']/median,
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

                if smoothType == 'roll_med':
                    smo = df_tbl['PDCSAP_FLUX'].rolling(s_window, center=True).median()

            if makefig:
                fig, axes = plt.subplots(figsize=(8,8))
                axes.errorbar(df_tbl['TIME'], df_tbl['PDCSAP_FLUX']/median,
                              yerr=df_tbl['PDCSAP_FLUX_ERR']/median, 
                              linestyle=None, alpha=0.15, label='PDCSAP_FLUX')
                axes.plot(df_tbl['TIME'], smo/median, label=str(s_window)+'pt median')

                if (acf_1dt > 0):
                    y = np.nanstd(smo/median)*acf_1pk*np.sin(df_tbl['TIME']/acf_1dt*2*np.pi) + 1
                    axes.plot(df_tbl['TIME'], y, lw=2, alpha=0.7,
                             label='ACF='+format(acf_1dt,'6.3f')+'d, pk='+format(acf_1pk,'6.3f'))

            if np.sum(ok_cut) < 1000:
                print('Warning: ' + f + ' contains < 1000 good points')

            sok_cut = np.isfinite(smo)

            FL = FINDflare((df_tbl['PDCSAP_FLUX'][sok_cut] - smo[sok_cut])/median, 
                           df_tbl['PDCSAP_FLUX_ERR'][sok_cut]/median,
                           avg_std=False, N1=4, N2=2, N3=5)

            for j in range(len(FL[0])):
                FL_id = np.append(FL_id, k)
                FL_t0 = np.append(FL_t0, df_tbl['TIME'][sok_cut].values[FL[0][j]])
                FL_t1 = np.append(FL_t1, df_tbl['TIME'][sok_cut].values[FL[1][j]])
                FL_f0 = np.append(FL_f0, median)
                s1, s2 = FL[0][j], FL[1][j]+1
                FL_f1 = np.append(FL_f1, np.nanmax(df_tbl['PDCSAP_FLUX'][sok_cut][s1:s2]))
                ed_val = np.trapz(df_tbl['PDCSAP_FLUX'][sok_cut][s1:s2],
                                  df_tbl['TIME'][sok_cut][s1:s2])
                FL_ed = np.append(FL_ed, ed_val)

                if makefig:
                    axes.scatter(df_tbl['TIME'][sok_cut][s1:s2],
                                 df_tbl['PDCSAP_FLUX'][sok_cut][s1:s2]/median,
                                 color='r', label='_nolegend_')
                    axes.scatter([],[], color='r', label='Flare?')        

            if makefig:
                axes.set_xlabel('Time [BJD - 2457000, days]')
                axes.set_ylabel('Normalized Flux')
                axes.legend()
                axes.set_title(filename)

                figname = filename + '.jpeg'
                makefig = ((not os.path.exists(figname)) | clobber)
                plt.savefig(figname, bbox_inches='tight', pad_inches=0.25, dpi=100)
                plt.close()      
        
        if progCounter:
            update_progress(k / len(files))
        
    ALL_TIC = pd.Series(files).str.split('-', expand=True).iloc[:,-3].astype('int')
    print(str(len(ALL_TIC[FL_id])) + ' flares found across ' + str(len(files)) + ' files')
    flare_out = pd.DataFrame(data={'TIC':ALL_TIC[FL_id],
                                   't0':FL_t0, 't1':FL_t1,
                                   'med':FL_f0, 'peak':FL_f1, 'ed':FL_ed})
    flare_out.to_csv(sector+ '_flare_out.csv')