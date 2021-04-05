#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Oct 28 15:35:48 2019

@author: casas100
"""

# from reactor_module import ReactorClass
import numpy as np
from scipy.linalg import svd
from itertools import cycle

import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator
from mpl_toolkits.axes_grid1 import make_axes_locatable

import pandas as pd
from PharmaPy.jac_module import numerical_jac_data
from PharmaPy import Gaussians as gs

from PharmaPy.LevMarq import levenberg_marquardt
from PharmaPy.Commons import plot_sens

from itertools import cycle
from ipopt import minimize_ipopt

linestyles = cycle(['-', '--', '-.', ':'])


def pseudo_inv(states, dstates_dparam=None):
    # Pseudo-inverse
    U, di, VT = np.linalg.svd(states)
    D_plus = np.zeros_like(states).T
    np.fill_diagonal(D_plus, 1 / di)
    pseudo_inv = np.dot(VT.T, D_plus).dot(U.T)

    if dstates_dparam is None:
        return pseudo_inv, di
    else:
        # Derivative
        first_term = -pseudo_inv @ dstates_dparam @ pseudo_inv
        second_term = pseudo_inv @ pseudo_inv.T @ dstates_dparam.T @ \
            (1 - states @ pseudo_inv)
        third_term = (1 - pseudo_inv @ states) @ (pseudo_inv.T @ pseudo_inv)

        dplus_dtheta = first_term + second_term + third_term

        return pseudo_inv, di, dplus_dtheta


def mcr_spectra(conc, spectra):
    conc_plus, _ = pseudo_inv(conc)

    absortivity_pred = np.dot(conc_plus, spectra)
    absorbance_pred = np.dot(conc, absortivity_pred)

    return conc_plus, absortivity_pred, absorbance_pred


class ParameterEstimation:

    """ Create a ParameterEstimation object

    Parameters
    ----------

    func : callable
        model function. Its signaure must be func(params, x_data, *args)
    params_seed : array-like
        parameter seed values
    x_data : numpy array
        1 x num_data array with experimental values for the independent
        variable.
    y_data : numpy array
        n_states x num_data experimental values for the dependent variable(s)
    """

    def __init__(self, func, param_seed, x_data, y_data=None, spectra=None,
                 fit_spectra=False, args_fun=None,
                 optimize_flags=None,
                 df_dtheta=None, df_dy=None, dx_finitediff=None,
                 measured_ind=None, covar_data=None,
                 name_params=None, name_states=None):

        self.function = func
        self.df_dtheta = df_dtheta
        self.dx_fd = dx_finitediff

        self.spectra = spectra
        self.fit_spectra = fit_spectra

        param_seed = np.asarray(param_seed)
        if param_seed.ndim == 0:
            param_seed = param_seed[np.newaxis]

        # --------------- Data
        if fit_spectra:
            y_fit = spectra
        else:  # TODO check this
            # if y_data.ndim == 1:
            #     y_data = y_data[..., np.newaxis]

            y_fit = y_data

        # If one dataset, then put it in an one-element list
        if not isinstance(y_fit, list):
            y_fit = [y_fit]
            x_data = [x_data]
        elif not isinstance(x_data, list):
            x_data = [x_data] * len(y_fit)

        if (covar_data is not None) and (type(covar_data) is not list):
            covar_data = [covar_data]

        self.num_datasets = len(y_fit)

        self.y_orig = []
        self.y_fit = []
        for data in y_fit:
            if data.ndim == 1:
                data = data[..., np.newaxis]

            self.y_orig.append(data)

            data[data == 0] = 1e-15
            self.y_fit.append(data.T.ravel())
            # self.y_fit.append(data)

        if args_fun is None:
            self.args_fun = [()] * self.num_datasets
        elif self.num_datasets == 1:
            self.args_fun = [args_fun]
        else:
            self.args_fun = args_fun

        if measured_ind is None:
            measured_ind = range(len(self.y_orig[0].T))

        self.measured_ind = measured_ind
        self.num_states = self.y_orig[0].shape[1]
        self.num_measured = len(measured_ind)

        self.num_xs = [len(xs) for xs in x_data]
        # self.num_data = [len(xs) * self.num_measured for xs in x_data]
        self.num_data = [array.size for array in y_fit]
        self.num_data_total = sum(self.num_data)

        self.x_data = x_data

        if covar_data is None:
            self.stdev_data = [np.ones(num_data) for num_data in self.num_data]
        else:
            self.stdev_data = [np.sqrt(covar.T.ravel())
                               for covar in covar_data]

        # --------------- Parameters
        self.num_params_total = len(param_seed)
        if optimize_flags is None:
            self.map_fixed = []
            self.map_variable = np.array([True]*self.num_params_total)
        else:
            self.map_variable = np.array(optimize_flags)
            self.map_fixed = ~self.map_variable

        # param_ind = range(len(param_seed))
        # self.ind_variable = [x for x in param_ind if x not in self.ind_fixed]

        self.param_seed = param_seed

        self.num_params = self.map_variable.sum()

        # --------------- Names
        # Parameters
        if name_params is None:
            self.name_params = ['theta_{}'.format(ind + 1)
                                for ind in range(self.num_params)]

            self.name_params_total = ['theta_{}'.format(ind + 1)
                                      for ind in range(self.num_params_total)]

            self.name_params_plot = [r'$\theta_{}$'.format(ind + 1)
                                     for ind in range(self.num_params)]
        else:
            self.name_params_total = name_params
            self.name_params = [name_params[ind]
                                for ind in range(len(name_params))
                                if self.map_variable[ind]]
            self.name_params_plot = [r'$' + name + '$'
                                     for name in self.name_params]

        # Outputs
        self.params_iter = []
        self.objfun_iter = []

        # States
        if name_states is None:
            self.name_states = [r'$y_{}$'.format(ind + 1)
                                for ind in range(self.num_states)]
        else:
            self.name_states = name_states

#        max_data = self.y_orig.max(axis=0)
#        min_data = self.y_orig.min(axis=0)
#
#        self.delta_conc = max_data - min_data
#        y_data = y_data.T.ravel()

        # Iteration-dependent output data
        self.cond_number = []

        # --------------- Outputs
        self.resid_runs = None
        self.y_runs = None
        self.sens = None
        self.sens_runs = None
        self.y_model = []

    def scale_sens(self, param_lims=None):
        """ Scale sensitivity matrix to make it non-dimensional.
        After Brun et al. Water Research, 36, 4113-4127 (2002),
        Jorke et al. Chem. Ing. Tech. 2015, 87, No. 6, 713-725,
        McLean et al. Can. J. Chem. Eng. 2012, 90, 351-366

        """

        ord_sens = self.reorder_sens(separate_sens=True)
        selected_sens = [ord_sens[ind] for ind in self.measured_ind]

        if param_lims is None:
            for ind, sens in enumerate(selected_sens):
                conc_time = self.conc_profile[:, ind][..., np.newaxis]
                sens *= self.params / conc_time
        else:
            for ind, sens in enumerate(selected_sens):
                delta_param = [par[1] - par[0] for par in param_lims]
                delta_param = np.array(delta_param)
                sens *= delta_param / self.delta_conc[ind]

        return np.vstack(selected_sens)

    def select_sens(self, sens_ordered, num_xs):
        parts = np.vsplit(sens_ordered, len(sens_ordered)//num_xs)

        selected = [parts[ind] for ind in self.measured_ind]
        selected_array = np.vstack(selected)

        return selected_array

    def reconstruct_params(self, params):
        params_reconstr = np.zeros(self.num_params_total)
        params_reconstr[self.map_fixed] = self.param_seed[self.map_fixed]
        params_reconstr[self.map_variable] = params

        return params_reconstr

    def objective_fun(self, params, residual_vec=False):

        # Reconstruct parameter set with fixed and non-fixed indexes
        params = self.reconstruct_params(params)

        # Store parameter values
        if type(self.params_iter) is list:
            self.params_iter.append(params)

        # --------------- Solve
        y_runs = []
        resid_runs = []
        sens_runs = []
        for ind in range(self.num_datasets):
            # Solve
            if self.fit_spectra:
                kwarg_sens = {'reorder': False}
            else:
                kwarg_sens = {}

            result = self.function(params, self.x_data[ind],
                                   *self.args_fun[ind],
                                   **kwarg_sens)

            if self.fit_spectra:
                if isinstance(result, tuple):
                    y_prof, sens_states = result
                else:
                    y_prof = result

                def func_aux(params, x_vals, *args):
                    states = self.function(params, x_vals, *args)

                    _, epsilon, absorbance = mcr_spectra(
                        states[:, self.measured_ind], self.spectra)

                    return absorbance.T.ravel()

                conc_target = y_prof[:, self.measured_ind]

                # MCR
                conc_plus = np.linalg.pinv(conc_target)
                absortivity_pred = np.dot(conc_plus, self.spectra[ind])
                y_prof = np.dot(conc_target, absortivity_pred)

                self.epsilon_mcr = absortivity_pred

                sens_analytical = True

                if sens_analytical:
                    eye = np.eye(conc_target.shape[0])
                    proj_orthogonal = eye - np.dot(conc_target, conc_plus)

                    sens_pick = sens_states[self.map_variable][
                        :, :, self.measured_ind]

                    first_term = proj_orthogonal @ sens_pick @ conc_plus
                    second_term = first_term.transpose((0, 2, 1))
                    sens_an = (first_term + second_term) @ y_prof

                    n_par, n_times, n_conc = sens_an.shape
                    sens = sens_an.T.reshape(n_conc * n_times, n_par)

                else:
                    args_merged = [self.x_data[ind], self.args_fun[ind]]
                    sens = numerical_jac_data(
                        func_aux, params,
                        args_merged,
                        dx=self.dx_fd)[:, self.map_variable]

            elif type(result) is tuple:  # func also returns the jacobian
                y_prof, sens = result

            else:  # call a separate function for jacobian
                y_prof = result

                if self.df_dtheta is None:
                    sens = numerical_jac_data(self.function, params,
                                              (self.x_data[ind], ),
                                              dx=self.dx_fd)
                else:
                    sens = self.df_dtheta(params, self.x_data[ind],
                                          *self.args_fun[ind])

            if y_prof.ndim == 1 or self.fit_spectra:
                y_run = y_prof
                sens_run = sens
            else:
                y_run = y_prof[:, self.measured_ind]
                sens_run = self.select_sens(sens, self.num_xs[ind])

            y_run = y_run.T.ravel()

            resid_run = (y_run - self.y_fit[ind])/self.stdev_data[ind]

            # Store
            y_runs.append(y_run)
            resid_runs.append(resid_run)
            sens_runs.append(sens_run)

        self.sens_runs = sens_runs
        self.y_runs = y_runs
        self.resid_runs = resid_runs

        if type(self.objfun_iter) is list:
            objfun_val = np.linalg.norm(np.concatenate(self.resid_runs))**2
            self.objfun_iter.append(objfun_val)

        residuals = self.optimize_flag * np.concatenate(resid_runs)
        self.residuals = residuals

        # Return objective
        if residual_vec:
            return residuals
        else:
            residual = 1/2 * residuals.dot(residuals)
            return residual

    def get_gradient(self, params, jac_matrix=False):
        if self.sens_runs is None:  # TODO: this is a hack to allow IPOPT
            self.objective_fun(params)

        concat_sens = np.vstack(self.sens_runs)
        if not self.fit_spectra:
            concat_sens = concat_sens[:, self.map_variable]

        self.sens = concat_sens

        # if type(self.cond_number) is list:
        #     self.cond_number.append(self.get_cond_number(concat_sens))

        std_dev = np.concatenate(self.stdev_data)
        jacobian = (concat_sens.T / std_dev)  # 2D

        if jac_matrix:
            return jacobian
        else:
            gradient = jacobian.dot(self.residuals)  # 1D
            return gradient

    def get_cond_number(self, sens_matrix):
        _, sing_vals, _ = np.linalg.svd(sens_matrix)

        cond_number = max(sing_vals) / min(sing_vals)

        return cond_number

    def optimize_fn(self, optim_options=None, simulate=False, verbose=True,
                    store_iter=True, method='LM', bounds=None):

        self.optimize_flag = not simulate
        params_var = self.param_seed[self.map_variable]

        if method == 'LM':
            self.opt_method = 'LM'
            if optim_options is None:
                optim_options = {'full_output': True, 'verbose': verbose}
            else:
                optim_options['full_output'] = True
                optim_options['verbose'] = verbose

            opt_par, inv_hessian, info = levenberg_marquardt(
                params_var,
                self.objective_fun,
                self.get_gradient,
                args=(True,),
                **optim_options)

        elif method == 'IPOPT':
            self.opt_method = 'IPOPT'
            if optim_options is None:
                optim_options = {'print_level': int(verbose) * 5}
            else:
                optim_options['print_level'] = int(verbose) * 5

            result = minimize_ipopt(self.objective_fun, params_var,
                                    jac=self.get_gradient,
                                    bounds=bounds, options=optim_options)

            opt_par = result['x']

            final_sens = np.vstack(self.sens_runs)[:, self.map_variable].T
            final_fun = np.concatenate(self.resid_runs)
            info = {'jac': final_sens, 'fun': final_fun}

        self.optim_options = optim_options

        # Store
        self.params_convg = opt_par
        # self.covar_params = inv_hessian
        self.info_opt = info

        self.cond_number = np.array(self.cond_number)
        self.params_iter = np.array(self.params_iter)
        _, idx = np.unique(self.params_iter, axis=0, return_index=True)

        if store_iter:
            self.params_iter = self.params_iter[np.sort(idx)]
            self.objfun_iter = np.array(self.objfun_iter)[np.sort(idx)]

            col_names = ['obj_fun'] + self.name_params_total
            self.paramest_df = pd.DataFrame(
                np.column_stack((self.objfun_iter, self.params_iter)),
                columns=col_names)

        # Model prediction with final parameters
        for ind in range(self.num_datasets):
            y_model_flat = self.resid_runs[ind]*self.stdev_data[ind] + \
                self.y_fit[ind]
            y_reshape = y_model_flat.reshape(-1, self.num_xs[ind]).T
            self.y_model.append(y_reshape)

        covar_params = self.get_covariance()

        return opt_par, covar_params, info

    def get_covariance(self):
        jac = self.info_opt['jac']
        resid = self.info_opt['fun']

        hessian_approx = np.dot(jac, jac.T)

        dof = self.num_data_total - self.num_params
        mse = 1 / dof * np.dot(resid, resid)

        covar = mse * np.linalg.inv(hessian_approx)

        # Correlation matrix
        sigma = np.sqrt(covar.diagonal())
        d_matrix = np.diag(1/sigma)
        correlation = d_matrix.dot(covar).dot(d_matrix)

        self.covar_params = covar
        self.correl_params = correlation

        return covar

    def inspect_data(self, fig_size=None):
        states_seed = []

        if self.fit_spectra:
            kwarg_sens = {'reorder': False}
        else:
            kwarg_sens = {}

        for ind in range(self.num_datasets):
            states_pred = self.function(self.param_seed, self.x_data[ind],
                                        *self.args_fun[ind],
                                        **kwarg_sens)

            if isinstance(states_pred, tuple):
                states_pred = states_pred[0]

            states_seed.append(states_pred)

        if len(states_seed) == 1:
            y_seed = states_seed[0]

            x_data = self.x_data[0]
            y_data = self.y_orig[0]

            fig, axes = plt.subplots(y_data.shape[1], figsize=fig_size)

            for ind, experimental in enumerate(y_data.T):
                axes[ind].plot(x_data, y_seed[:, self.measured_ind[ind]])
                axes[ind].plot(x_data, experimental, 'o', mfc='None')

                axes[ind].spines['right'].set_visible(False)
                axes[ind].spines['top'].set_visible(False)

                axes[ind].set_ylabel(self.name_states[ind])

            axes[0].legend(
                ('prediction with seed params', 'experimental data'))
            axes[-1].set_xlabel('$x$')

        else:
            pass  # TODO what to do with multiple datasets, maybe a parity plot?

    def plot_data_model(self, fig_size=None, fig_grid=None, fig_kwargs=None,
                        plot_initial=False, black_white=False, x_div=1):

        num_plots = self.num_datasets

        if fig_grid is None:
            num_cols = bool(num_plots // 2) + 1
            num_rows = num_plots // 2 + num_plots % 2
        else:
            num_cols = fig_grid[1]
            num_rows = fig_grid[0]

        fig, axes = plt.subplots(num_rows, num_cols, figsize=fig_size)

        if num_plots == 1:
            axes = np.asarray(axes)[np.newaxis]

        if fig_kwargs is None:
            fig_kwargs = {'mfc': 'None', 'ls': '', 'ms': 3}

        ax_flatten = axes.flatten()
        names_meas = [self.name_states[ind] for ind in self.measured_ind]
        # params_nominal = self.reconstruct_params(self.params_convg)

        for ind in range(self.num_datasets):
            # Experimental data
            x_exp = self.x_data[ind] / x_div
            y_exp = self.y_orig[ind]
            markers = cycle(['o', 's', '^', '*', 'P', 'X'])

            # Model prediction
            if black_white:
                ax_flatten[ind].plot(x_exp, self.y_model[ind], 'k')

                for col in y_exp.T:
                    ax_flatten[ind].plot(x_exp, col, color='k',
                                         marker=next(markers), **fig_kwargs)
            else:
                ax_flatten[ind].plot(x_exp, self.y_model[ind])
                lines = ax_flatten[ind].lines
                colors = [line.get_color() for line in lines]

                for color, col in zip(colors, y_exp.T):
                    ax_flatten[ind].plot(x_exp, col, color=color,
                                         marker=next(markers), **fig_kwargs)

            # Edit
            ax_flatten[ind].spines['right'].set_visible(False)
            ax_flatten[ind].spines['top'].set_visible(False)

            ax_flatten[ind].set_xlabel('$x$')
            ax_flatten[ind].set_ylabel(r'$\mathbf{y}$')

            ax_flatten[ind].xaxis.set_minor_locator(AutoMinorLocator(2))
            ax_flatten[ind].yaxis.set_minor_locator(AutoMinorLocator(2))

        ax_flatten[0].legend(names_meas, loc='best')

        if plot_initial:
            residuals_convg = self.residuals.copy()
            resid_runs_convg = self.resid_runs.copy()

            seed_params = self.param_seed[self.map_variable]
            resid_seed = self.objective_fun(seed_params, residual_vec=True)
            resid_seed = np.split(resid_seed, self.num_datasets)

            for ind in range(self.num_datasets):
                ymodel_seed = resid_seed[ind] + self.y_fit[ind]
                ymodel_seed = ymodel_seed.reshape(-1, self.num_xs[ind])

                markers = cycle(['o', 's', '^', '*', 'P', 'X'])
                fig_kwargs['ls'] = '-'
                if black_white:
                    for rowind, col in enumerate(ymodel_seed):
                        ax_flatten[ind].plot(x_exp, col, '--',
                                             color='k',
                                             marker=next(markers), ms=3,
                                             alpha=0.3, **fig_kwargs)
                        # markevery=3)
                else:
                    for rowind, col in enumerate(ymodel_seed):
                        ax_flatten[ind].plot(x_exp, col, '--',
                                             marker=next(markers),
                                             color=colors[rowind],
                                             alpha=0.3, **fig_kwargs)
                        # markevery=3)

                self.resid_runs = resid_runs_convg
                self.residuals = residuals_convg

        if len(ax_flatten) > self.num_datasets:
            fig.delaxes(ax_flatten[-1])

        if len(axes) == 1:
            axes = axes[0]

        return fig, axes

    def plot_data_model_sep(self, fig_size=None, fig_kwargs=None,
                            plot_initial=False):

        if len(self.x_data) > 1:
            raise NotImplementedError('More than one dataset detected. '
                                      'Not supported')

        xdata = self.x_data[0]
        ydata = self.y_orig[0]
        ymodel = self.y_model[0]

        num_x = self.num_xs[0]
        num_plots = self.num_states

        num_col = 2
        num_row = num_plots // num_col + num_plots % num_col

        fig, axes = plt.subplots(num_row, num_col, figsize=fig_size)

        for ind in range(num_plots):
            axes.flatten()[ind].plot(xdata, ymodel[:, ind])

            line = axes.flatten()[ind].lines[0]
            color = line.get_color()
            axes.flatten()[ind].plot(xdata, ydata[:, ind], 'o', color=color,
                                     mfc='None')

            axes.flatten()[ind].set_ylabel(self.name_states[ind])

        if plot_initial:
            residuals_convg = self.residuals.copy()
            resid_runs_convg = self.resid_runs.copy()

            seed_params = self.param_seed[self.map_variable]
            resid_seed = self.objective_fun(seed_params, residual_vec=True)

            ymodel_seed = resid_seed*self.stdev_data[0] + self.y_fit[0]
            ymodel_seed = ymodel_seed.reshape(-1, num_x)
            for ind in range(num_plots):
                axes.flatten()[ind].plot(xdata, ymodel_seed[ind], '--',
                                         color=color,
                                         alpha=0.4)

            self.resid_runs = resid_runs_convg
            self.residuals = residuals_convg

        # fig.text(0.5, 0, '$x$', ha='center')
        fig.tight_layout()
        return fig, axes

    def plot_sens_param(self):
        figs = []
        axes = []
        for times, sens in zip(self.x_data, self.sens_runs):
            fig, ax = plot_sens(times, sens)

            figs.append(fig)
            axes.append(ax)

        return figs, axes

    def plot_parity(self, fig_size=(4.5, 4.0), fig_kwargs=None):
        if fig_kwargs is None:
            fig_kwargs = {'alpha': 0.70}

        fig, axis = plt.subplots(figsize=fig_size)

        for ind, y_model in enumerate(self.y_model):
            axis.scatter(y_model.T.flatten(), self.y_fit[ind],
                         label='experiment {}'.format(ind + 1),
                         **fig_kwargs)

        axis.set_xlabel('Model')
        axis.set_ylabel('Data')
        axis.legend(loc='best')

        all_data = np.concatenate((np.concatenate(self.y_runs),
                                   np.concatenate(self.y_fit)))
        plot_min = min(all_data)
        plot_max = max(all_data)

        offset = 0.05*plot_max
        x_central = [plot_min - offset, plot_max + offset]

        axis.plot(x_central, x_central, 'k')
        axis.set_xlim(x_central)
        axis.set_ylim(x_central)

        return fig, axis

    def plot_correlation(self):

        # Mask
        mask = np.tri(self.num_params, k=-1).T
        corr_masked = np.ma.masked_array(self.correl_params, mask=mask)

        # Plot
        fig_heat, axis_heat = plt.subplots()

        heatmap = axis_heat.imshow(corr_masked, cmap='RdBu', aspect='equal',
                                   vmin=-1, vmax=1)
        divider = make_axes_locatable(axis_heat)
        cax = divider.append_axes("right", size="5%", pad=0.05)

        cbar = fig_heat.colorbar(heatmap, ax=axis_heat, cax=cax)
        cbar.outline.set_visible(False)

        axis_heat.set_xticks(range(self.num_params))
        axis_heat.set_yticks(range(self.num_params))

        axis_heat.set_xticklabels(self.name_params_plot, rotation=90)
        axis_heat.set_yticklabels(self.name_params_plot)

        return fig_heat, axis_heat


class Deconvolution:
    def __init__(self, mu, sigma, ampl, x_data, y_data):

        self.mu = mu
        self.sigma = sigma
        self.ampl = ampl

        self.x_data = x_data
        self.y_data = y_data

    def concat_params(self):
        if isinstance(self.mu, float) or isinstance(self.mu, int):
            params_concat = np.array([self.mu, self.sigma, self.ampl])
        else:
            params_concat = np.concatenate((self.mu, self.sigma, self.ampl))

        return params_concat

    def fun_wrapper(self, params, x_data):

        grouped_params = np.split(params, 3)
        gaussian = gs.multiple_gaussian(self.x_data, *grouped_params)

        return gaussian

    def dparam_wrapper(self, params, x_data):
        grouped_params = np.split(params, 3)
        der_params = gs.gauss_dparam_mult(x_data, *grouped_params)

        return der_params

    def dx_wrapper(self, params, x_data):
        grouped_params = np.split(params, 3)
        der_x = gs.gauss_dx_mult(x_data, *grouped_params)

        return der_x

    def inspect_data(self):
        gaussian_pred = gs.multiple_gaussian(self.x_data, self.mu, self.sigma,
                                             self.ampl)

        fig, axis = plt.subplots()

        axis.plot(self.x_data, gaussian_pred)
        axis.plot(self.x_data, self.y_data, 'o', mfc='None')

        axis.legend(('prediction with seed params', 'experimental data'))
        axis.set_xlabel('$x$')
        axis.set_ylabel('signal')

        axis.spines['right'].set_visible(False)
        axis.spines['top'].set_visible(False)

        return fig, axis, gaussian_pred

    def estimate_params(self, optim_opt=None):
        seed = self.concat_params()

        paramest = ParameterEstimation(self.fun_wrapper, seed, self.x_data,
                                       self.y_data,
                                       df_dtheta=self.dparam_wrapper,
                                       df_dy=self.dx_wrapper)

        result = paramest.optimize_fn(optim_options=optim_opt)

        optim_params = np.split(result[0], 3)

        self.param_obj = paramest
        self.optim_params = optim_params

        return optim_params, result[1:], paramest

    def plot_results(self, fig_size=None, plot_initial=False,
                     plot_individual=False):

        fig, axis = self.param_obj.plot_data_model(fig_size=fig_size,
                                                   plot_initial=plot_initial)

        axis.legend(('best fit', 'experimental data'))
        axis.set_ylabel('signal')

        axis.xaxis.set_minor_locator(AutoMinorLocator(2))
        axis.yaxis.set_minor_locator(AutoMinorLocator(2))

        if plot_individual:
            gaussian_vals = gs.multiple_gaussian(
                self.x_data, *self.optim_params, separated=True)

            for row in gaussian_vals.T:
                axis.plot(self.x_data, row, '--')
                color = axis.lines[-1].get_color()

                axis.fill_between(self.x_data, row.min(), row, fc=color,
                                  alpha=0.2)

        return fig, axis

    def plot_deriv(self, which='both', plot_mu=False):
        fig, axis = plt.subplots()

        # fun = gs.multiple_gaussian(self.x_data, *self.optim_params)

        if which == 'both':
            first = gs.gauss_dx_mult(self.x_data, *self.optim_params)
            second = gs.gauss_dxdx_mult(self.x_data, *self.optim_params)

            axis.plot(self.x_data, first)
            axis.set_ylabel('$\partial f / \partial x$')

            axis_sec = axis.twinx()
            axis_sec.plot(self.x_data, second, '--')
            axis_sec.set_ylabel('$\partial^2 f / \partial x^2$')

            fig.legend(('first', 'second'), bbox_to_anchor=(1, 1),
                       bbox_transform=axis.transAxes)

            axis.spines['top'].set_visible(False)
            axis_sec.spines['top'].set_visible(False)

        elif which == 'first':
            first = gs.gauss_dx_mult(self.x_data, *self.optim_params)
            axis.plot(self.x_data, first)
            axis.set_ylabel('$\partial f / \partial x$')

            axis.spines['right'].set_visible(False)
            axis.spines['top'].set_visible(False)

        elif which == 'second':
            second = gs.gauss_dxdx_mult(self.x_data, *self.optim_params)
            axis.plot(self.x_data, second)
            axis.set_ylabel('$\partial^2 f / \partial x^2$')

            axis.spines['right'].set_visible(False)
            axis.spines['top'].set_visible(False)

        axis.xaxis.set_minor_locator(AutoMinorLocator(2))
        axis.yaxis.set_minor_locator(AutoMinorLocator(2))

        axis.set_xlabel('$x$')

        if plot_mu:
            mu_opt = self.optim_params[0]

            for mu in mu_opt:
                axis.axvline(mu, ls='--', alpha=0.4)

        return fig, axis


if __name__ == '__main__':
    import englezos_example as englezos

    # Data
    data = np.genfromtxt('../data/englezos_example.csv', delimiter=',',
                         skip_header=1)
    t_exp, c3_exp = data.T

    init_conc = [60, 60, 0]
    param_seed = [1e-5, 1e-5]
#    param_seed = [0.4577e-5, 0.2797e-3]

    reaction_matrix = np.array([-2, -1, 2])
    species = ('$NO$', '$O_2$', '$NO_2$')

    param_object = ParameterEstimation(
        reaction_matrix, param_seed,
        t_exp, c3_exp,
        y_init=init_conc,
        measured_ind=(-1,),
        kinetic_model=englezos.bodenstein_linder,
        df_dstates=englezos.jac_conc,
        df_dtheta=englezos.jac_par,
        names_species=species)

    simulate = True

    if simulate:
        param_object.solve_model(init_conc, x_eval=t_exp, eval_sens=True)
        param_object.plot_states()
        fig_sens, axes_sens = param_object.plot_sens(fig_size=(5, 2))

        sens_total = param_object.reorder_sens()
        U, sing_vals, V = svd(sens_total)
        cond_number = max(sing_vals) / min(sing_vals)

        labels = list('ab')
        for ax, lab in zip(axes_sens, labels):
            ax.text(0.05, 0.9, lab, transform=ax.transAxes)

        fig_sens.savefig('../img/sens_englezos.pdf', bbox_inches='tight')

    else:
        optim_options = {'max_iter': 150, 'full_output': True, 'tau': 1e-2}

        params_optim, covar, info = param_object.optimize_fn(
            optim_options=optim_options)
        param_object.plot_data_model()
