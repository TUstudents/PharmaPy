import numpy as np
from assimulo.problem import Implicit_Problem
from PharmaPy.Phases import classify_phases
from PharmaPy.Streams import VaporStream
from PharmaPy.Connections import get_inputs_new
from PharmaPy.Commons import unpack_discretized
from PharmaPy.Streams import LiquidStream
from PharmaPy.Results import DynamicResult
from PharmaPy.Plotting import plot_distrib

from assimulo.solvers import IDA

import scipy.optimize
import scipy.sparse

from itertools import cycle
from matplotlib.ticker import AutoMinorLocator, MaxNLocator


class DistillationColumn:
    def __init__(self, col_P, q_feed, LK, HK,
                 per_LK, per_HK, reflux=None, num_plates=None,
                 gamma_model='ideal', N_feed=None):

        self.num_plates = num_plates
        self.reflux = reflux
        self.q_feed = q_feed
        self.col_P = col_P

        self.LK = LK
        self.HK = HK
        self.per_HK = per_HK
        self.per_LK = per_LK

        self.gamma_model = gamma_model

        self.N_feed = N_feed  # Num plate from bottom
        self.per_NLK = 100  # Sharp split all NLK recovred in distillate
        self.per_NHK = 0  # Sharp split no NHK in distillate

        self._Inlet = None
        self._Phases = None
        return

    def nomenclature(self):
        self.name_states = []
        self.names_states_out = []
        self.names_states_in = self.names_states_out
        self.states_di = {
            'num_plates': {'dim': 1},
            'x': {'dim': len(self.name_species), 'index': self.name_species},
            'y': {'dim': len(self.name_species),  'index': self.name_species},
            'T': {'dim': 1, 'units': 'K'},
            'bot_flowrate': {'dim': 1, 'units': 'mole/sec'},
            'dist_flowrate': {'dim': 1, 'units': 'mole/sec'},
            'reflux': {'dim': 1},
            'N_feed': {'dim': 1}, 'x_dist': {'dim': len(self.name_species)},
            'x_bot': {'dim': len(self.name_species)},
            'min_reflux': {'dim': 1}, 'N_min': {'dim': 1}
        }

        num_comp = len(self.Inlet.name_species)
        len_in = [1, num_comp]

        if self.names_states_in:
            states_in_dict = dict(zip(self.names_states_in, len_in))
        else:
            states_in_dict = []
        self.states_in_dict = {'Inlet': states_in_dict}

    @property
    def Inlet(self):
        return self._Inlet

    @Inlet.setter
    def Inlet(self, inlet):
        self._Inlet = inlet
        self._Inlet.pres = self.col_P
        self.feed_flowrate = inlet.mole_flow
        self.z_feed = inlet.mole_frac

        name_species = self.Inlet.name_species
        self.num_species = len(name_species)
        self.LK_index = name_species.index(self.LK)
        self.HK_index = name_species.index(self.HK)

        self.name_species = name_species

        self.nomenclature()

    def get_inputs(self, time):
        inputs = get_inputs_new(time, self.Inlet, self.states_in_dict)

        return inputs

    def estimate_comp(self, name_species, feed_flowrate, z_feed, LK, HK, LK_index, HK_index):
        # Determine Light Key and Heavy Key component numbers

        bubble_pure = self.Inlet.AntoineEquation(pres=self.col_P)
        volatility_order = np.argsort(bubble_pure)

        hk_loc = np.where(volatility_order == HK_index)[0][0]
        lk_loc = np.where(volatility_order == LK_index)[0][0]

        if hk_loc != lk_loc + 1:
            print('High key and low key indices are not adjacent')

        # Calculate Distillate and Bottom flow rates
        bot_flowrate = (feed_flowrate*z_feed[HK_index]*(1-self.per_HK/100)
                        + feed_flowrate*z_feed[LK_index]*(1-self.per_LK/100)
                        + sum(feed_flowrate *
                              z_feed[:LK_index])*(1-self.per_NLK/100)
                        + sum(feed_flowrate*z_feed[HK_index+1:])*(1-self.per_NHK/100))
        dist_flowrate = feed_flowrate - bot_flowrate

        if bot_flowrate < 0 or dist_flowrate < 0:
            print('negative flow rates, given value not feasible')

        # Estimate component fractions
        x_dist = np.zeros_like(z_feed)
        x_bot = np.zeros_like(z_feed)

        x_bot[:LK_index] = (sum(feed_flowrate*z_feed[:LK_index])
                            * (1-self.per_NLK/100)/bot_flowrate)
        x_bot[LK_index] = (feed_flowrate*z_feed[LK_index]
                           * (1-self.per_LK/100)/bot_flowrate)
        x_bot[HK_index] = (feed_flowrate*z_feed[HK_index]
                           * (1-self.per_HK/100)/bot_flowrate)
        x_bot[HK_index+1:] = (sum(feed_flowrate*z_feed[HK_index+1:])
                              * (1-self.per_NHK/100)/bot_flowrate)

        x_dist = (feed_flowrate*z_feed - bot_flowrate*x_bot)/dist_flowrate

        # Fenske equation
        k_vals_bot = self._Inlet.getKeqVLE(pres=self.col_P, x_liq=x_bot)
        k_vals_dist = self._Inlet.getKeqVLE(pres=self.col_P, x_liq=x_dist)
        alpha_fenske = (k_vals_dist[LK_index]/k_vals_dist[HK_index] *
                        k_vals_bot[LK_index]/k_vals_bot[HK_index])**0.5
        N_min = (np.log(self.per_LK/100/(1-self.per_LK/100) /
                        ((self.per_HK/100)/(1-self.per_HK/100)))
                 / np.log(alpha_fenske))
        self.N_min = N_min
        return x_dist, x_bot, dist_flowrate, bot_flowrate

    def get_k_vals(self, x_oneplate=None, temp=None):
        if x_oneplate is None:
            x_oneplate = self.z_feed
        k_vals = self._Inlet.getKeqVLE(pres=self.col_P, temp=temp,
                                       x_liq=x_oneplate)
        return k_vals

    def VLE(self, y_oneplate=None, temp=None, need_x_vap=True):
        # VLE uses vapor stream, need vapor stream object temporarily.
        temporary_vapor = VaporStream(path_thermo=self._Inlet.path_data,
                                      pres=self.col_P, mole_flow=self.feed_flowrate, mole_frac=y_oneplate)
        res = temporary_vapor.getDewPoint(pres=self.col_P, mass_frac=None,
                                          mole_frac=y_oneplate, thermo_method=self.gamma_model, x_liq=need_x_vap)
        # Program needs VLE function to return output in x,Temp format
        return res[::-1]

    def calc_reflux(self, x_dist, x_bot, dist_flowrate, bot_flowrate,
                    reflux, num_plates, pres):
        # Calculate operating lines
        LK_index = self.LK_index
        HK_index = self.HK_index
        k_vals = self.get_k_vals(x_oneplate=self.z_feed)

        alpha = k_vals/k_vals[HK_index]
        # Estimate Reflux ratio
        # First Underwood equation
        # (1-q is 0), Feed flow rate cancelled from both sides, 10^-10 is to avoid division by 0

        def f(phi): return (sum(alpha * self.z_feed /
                                (alpha - phi + np.finfo(float).eps)))**2
        bounds = ((alpha[HK_index], alpha[LK_index]),)
        phi = scipy.optimize.minimize(
            f, (alpha[LK_index] + alpha[HK_index])/2, bounds=bounds, tol=10**-10)
        phi = phi.x
        # Second underwood equation
        V_min = sum(alpha*dist_flowrate*x_dist/(alpha-phi))
        L_min = V_min - dist_flowrate
        min_reflux = L_min/dist_flowrate
        self.min_reflux = min_reflux

        if reflux is None or reflux == 0:
            reflux = 1.5*min_reflux  # Heuristic
        if reflux < 0:
            reflux = -1*reflux*self.min_reflux
        elif reflux > 0 and reflux < self.min_reflux:
            print(
                'Specified reflux less than min_reflux, calculation proceeds with 1.5*min_reflux')
            reflux = 1.5*self.min_reflux

        if num_plates:
            def bot_comp_err(reflux, num_plates, x_dist, x_bot,
                             dist_flowrate, bot_flowrate):

                Ln = reflux*dist_flowrate
                Vn = Ln + dist_flowrate
                # Stripping section
                Lm = Ln + self.feed_flowrate + \
                    self.feed_flowrate*(self.q_feed-1)
                Vm = Lm - bot_flowrate
                # Calculate compositions
                x = np.zeros((num_plates+1, self.num_species))
                y = np.zeros((num_plates+1, self.num_species))
                T = np.zeros(num_plates+1)
                y[0] = x_dist
                x_new, T_new = self.VLE(y[0])
                x[0] = x_new
                T[0] = T_new
                if self.N_feed:  # Feed plate specified
                    for i in range(1, num_plates+1):
                        # Rectifying section
                        if i < self.N_feed+1:
                            y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                            x_new, T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new

                        # Stripping section
                        else:
                            y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                            x_new, T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new

                else:  # feed plate not specified
                    y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                    y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)

                    for i in range(1, num_plates+1):
                        # Rectifying section
                        if (y_top_op[LK_index]/y_top_op[HK_index] < y_bot_op[LK_index]/y_bot_op[HK_index]):

                            y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                            x_new, T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new

                            y_bot_op = (np.array(x_new)*Lm /
                                        Vm - (Lm/Vm-1)*x_bot)
                            y_top_op = (np.array(x_new)*Ln /
                                        Vn + (1-Ln/Vn)*x_dist)

                        # Stripping section
                        else:
                            y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                            x_new, T_new = self.VLE(y[i])
                            x[i] = x_new
                            T[i] = T_new

                # pentalty for very high reflux values
                error = np.linalg.norm(
                    x_bot - x[-1])/np.linalg.norm(x_bot)*100 + 0.01 * reflux**2
                return error

            reflux = scipy.optimize.minimize(bot_comp_err, x0=1.5*self.min_reflux,
                                             args=(num_plates, x_dist, x_bot,
                                                   dist_flowrate, bot_flowrate),
                                             method='Nelder-Mead', bounds=((1.01*self.min_reflux, 1000),))
            reflux = reflux.x
        self.reflux = reflux
        return reflux

    def calc_plates(self, x_dist, x_bot, dist_flowrate, bot_flowrate, reflux, num_plates):
        LK_index = self.LK_index
        HK_index = self.HK_index

        # Calculate Vapour and Liquid flows in column
        # Rectifying section
        Ln = reflux*dist_flowrate
        Vn = Ln + dist_flowrate

        # Stripping section
        Lm = Ln + self.feed_flowrate + self.feed_flowrate*(self.q_feed-1)
        Vm = Lm - bot_flowrate

        if num_plates is None:
            # Calculate number of plates
            # Composition list
            x = []
            y = []
            T = []
            counter1 = 1
            counter2 = 1
            # more likely to have non LKs and no non HKs
            # start counting from top of column
            # First plate
            y.append(x_dist)
            x_new, T_new = self.VLE(y[0])
            x.append(x_new)
            T.append(T_new)

            y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
            y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)

            # Rectifying section
            while (y_top_op[LK_index]/y_top_op[HK_index] < y_bot_op[LK_index]/y_bot_op[HK_index]):
                y.append((np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist))
                x_new, T_new = self.VLE(y[-1])
                x.append(x_new)
                T.append(T_new)

                if counter2 > 100:
                    break
                counter2 += 1
                y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)

            # Feed plate
            N_feed = counter2

            # Stripping section
            # When reflux is specified and num_plates is not calculated x returned contains one extra evaluation, not true for case when num_plates is specified. This is why fudge factors are added
            while (np.array(x[-1][HK_index]) < 0.98*x_bot[HK_index] or np.array(x[-1][LK_index]) > 1.2*x_bot[LK_index]):
                y.append((np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot))
                x_new, T_new = self.VLE(y[-1])
                x.append(x_new)
                T.append(T_new)
                if counter1 > 100:
                    break
                counter1 += 1

            num_plates = len(y)-1  # Remove distillate stream, reboiler

        else:  # Num plates specified
            # Calculate compositions
            x = np.zeros((num_plates+1, self.num_species))
            y = np.zeros((num_plates+1, self.num_species))
            T = np.zeros(num_plates+1)

            y[0] = x_dist
            x_new, T_new = self.VLE(y[0])
            x[0] = x_new
            T[0] = T_new

            if self.N_feed is None:  # Feed plate not specified
                y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                flag_rect_section_present = 0

                for i in range(1, num_plates+1):
                    # Rectifying section
                    if (y_top_op[LK_index]/y_top_op[HK_index] < y_bot_op[LK_index]/y_bot_op[HK_index]):

                        y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                        x_new, T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new

                        y_bot_op = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                        y_top_op = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                        N_feed = i  # To get feed plate, only last value used, cant write outside if cause elif structure will be violated
                        flag_rect_section_present = 1

                    # Stripping section
                    elif (np.array(x[-1][HK_index]) < 0.98*x_bot[HK_index] or np.array(x[-1][LK_index]) > 1.2*x_bot[LK_index]):
                        y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                        x_new, T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new
                        if not(flag_rect_section_present):
                            N_feed = num_plates
            else:  # Feed plate specified
                N_feed = self.N_feed
                for i in range(1, num_plates+1):
                    # Rectifying section
                    if i < self.N_feed:
                        y[i] = (np.array(x_new)*Ln/Vn + (1-Ln/Vn)*x_dist)
                        x_new, T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new
                        N_feed = self.N_feed
                    # Stripping section
                    else:
                        y[i] = (np.array(x_new)*Lm/Vm - (Lm/Vm-1)*x_bot)
                        x_new, T_new = self.VLE(y[i])
                        x[i] = x_new
                        T[i] = T_new

        self.N_feed = N_feed

        self.retrieve_results(num_plates, x, y, T, bot_flowrate, dist_flowrate,
                              reflux, N_feed, x_dist, x_bot, self.min_reflux, self.N_min)
        return num_plates

    def solve_unit(self, runtime=None, t0=0):
        x_dist, x_bot, dist_flowrate, bot_flowrate = self.estimate_comp(self.name_species, self.feed_flowrate, self.z_feed,
                                                                        self.LK, self.HK, self.LK_index, self.HK_index)
        reflux = self.calc_reflux(x_dist, x_bot, dist_flowrate, bot_flowrate,
                                  self.reflux, self.num_plates, self.col_P)
        num_plates = self.calc_plates(
            x_dist, x_bot, dist_flowrate, bot_flowrate, reflux, self.num_plates)
        return

    def retrieve_results(self, num_plates, x, y, T, bot_flowrate, dist_flowrate, reflux, N_feed, x_dist, x_bot, min_reflux, N_min):

        if not(isinstance(x, np.ndarray)):
            x = np.array(x)
            y = np.array(y)
            T = np.array(T)
        dist_result = {'num_plates': num_plates, 'x': x.T, 'y': y.T, 'T': T,
                       'bot_flowrate': bot_flowrate, 'dist_flowrate': dist_flowrate, 'reflux': reflux,
                       'N_feed': N_feed, 'x_dist': x_dist, 'x_bot': x_bot, 'min_reflux': min_reflux, 'N_min': N_min
                       }

        self.result = DynamicResult(self.states_di, **dist_result)

        path = self.Inlet.path_data
        self.OutletDistillate = LiquidStream(path, temp=dist_result['T'][0],
                                             mole_conc=dist_result['x_dist'],
                                             mole_flow=dist_result['dist_flowrate'])
        self.OutletBottom = LiquidStream(path, temp=dist_result['T'][-1],
                                         mole_conc=dist_result['x_bot'],
                                         mole_flow=dist_result['bot_flowrate'])
        self.Outlet = self.OutletBottom


class DynamicDistillation():
    def __init__(self, col_P, q_feed, LK, HK,
                 per_LK, per_HK, reflux=None, num_plates=None,
                 gamma_model='ideal', N_feed=None):

        self.num_plates = num_plates

        self.reflux = reflux
        self.q_feed = q_feed
        self.col_P = col_P
        self.LK = LK
        self.HK = HK
        self.per_HK = per_HK
        self.per_LK = per_LK

        self.gamma_model = gamma_model
        self.N_feed = N_feed  # Num plate from bottom

        self._Phases = None
        self._Inlet = None

        self.oper_mode = 'Continuous'

    def nomenclature(self):
        self.name_states = ['temp', 'mole_frac']
        self.names_states_out = ['temp', 'mole_frac']
        self.names_states_in = self.names_states_out
        # self.names_states_in.append('vol_flow')
        self.states_di = {
            'temp': {'dim': 1, 'units': 'K'},
            'mole_frac': {'dim': len(self.name_species), 'index': self.name_species},
        }

        self.fstates_di = {}

    @property
    def Phases(self):
        return self._Phases

    @Phases.setter
    def Phases(self, phases):
        if not isinstance(phases, (list, tuple)):
            phases = [phases]

        self._Phases = phases

        classify_phases(self)

        self.holdup = self.Liquid_1.moles

        name_species = self.Liquid_1.name_species
        self.num_species = len(name_species)
        self.LK_index = name_species.index(self.LK)
        self.HK_index = name_species.index(self.HK)

        self.name_species = name_species

        self.nomenclature()

    @property
    def Inlet(self):
        return self._Inlet

    @Inlet.setter
    def Inlet(self, inlet):
        self._Inlet = inlet
        self.feed_flowrate = inlet.mole_flow
        self.z_feed = inlet.mole_frac

        num_comp = self.num_species
        self.len_in = [1, num_comp]  # , 1]
        self.len_out = [1, num_comp]
        states_in_dict = dict(zip(self.names_states_in, self.len_in))
        states_out_dict = dict(zip(self.names_states_out, self.len_out))

        self.states_in_dict = {'Inlet': states_in_dict}
        self.states_out_dict = {'Outlet': states_out_dict}
        self.column_startup()

        name_species = self.Inlet.name_species

        self.num_species = len(name_species)
        self.LK_index = name_species.index(self.LK)
        self.HK_index = name_species.index(self.HK)
        self.name_species = name_species

    def get_inputs(self, time):
        inputs = get_inputs_new(time, self.Inlet, self.states_in_dict)
        return inputs

    def column_startup(self):
        # Total reflux conditions (Startup)
        # Steady state values (Based on steady state column)
        column_user_inputs = {
            'col_P': self.col_P,  # Pa
            'num_plates': self.num_plates,  # exclude reboiler
            'reflux': self.reflux,  # L/D
            'q_feed': self.q_feed,  # Feed q value
            'LK': self.LK,  # LK
            'HK': self.HK,  # HK
            'per_LK': self.per_LK,  # % recovery LK in distillate
            'per_HK': self.per_HK,  # % recovery HK in distillate
            # 'holdup': self.holdup,
            'N_feed': self.N_feed
                              }

        steady_col = DistillationColumn(**column_user_inputs)
        steady_col.Inlet = self._Inlet
        steady_col.solve_unit()

        # # Calculate compositions for starup at total reflux
        # column_total_reflux = {
        #     'col_P': self.col_P,  # Pa
        #     'num_plates': steady_col.result.num_plates,  # exclude reboiler
        #     'reflux': 1e5,  # L/D
        #     'q_feed': self.q_feed,  # Feed q value
        #     'LK': self.LK,  # LK
        #     'HK': self.HK,  # HK
        #     'per_LK': self.per_LK,  # % recovery LK in distillate
        #     'per_HK': self.per_HK,  # % recovery HK in distillate
        #     # 'holdup': self.holdup,
        #     'N_feed': steady_col.result.N_feed
        #                        }

        # total_reflux_col = DistillationColumn(**column_total_reflux)
        # total_reflux_col.Inlet = self._Inlet
        # total_reflux_col.solve_unit()

        # self.x0 = total_reflux_col.result.x.T
        # self.y0 = total_reflux_col.result.y.T
        # self.T0 = total_reflux_col.result.T
        self.num_plates = steady_col.result.num_plates
        self.bot_flowrate = steady_col.result.bot_flowrate
        self.dist_flowrate = steady_col.result.dist_flowrate
        self.reflux = steady_col.result.reflux
        self.N_feed = steady_col.result.N_feed
        self.x_dist = steady_col.result.x_dist
        self.x_bot = steady_col.result.x_bot
        self.min_reflux = steady_col.result.min_reflux
        self.N_min = steady_col.result.N_min

    def unit_model(self, time, states, d_states):
        '''This method will work by itself and does not need any user manipulation.
        Fill material and energy balances with your model.'''
        di_states = unpack_discretized(states, self.len_out,
                                       self.name_states)

        material = self.material_balances(time, **di_states)

        di_d_states = unpack_discretized(d_states, self.len_out,
                                         self.name_states)
        # N_plates(N_components), only for compositions
        material[:, 1:] = material[:, 1:] - di_d_states['mole_frac']
        balances = material.ravel()
        return balances

    def material_balances(self, time, temp, mole_frac):
        x = mole_frac
        inputs = self.get_inputs(time)['Inlet']
        z_feed = inputs['mole_frac']

        # GET STARTUP CONDITIONS
        (bot_flowrate, dist_flowrate,
         reflux, N_feed, M_const) = (self.bot_flowrate, self.dist_flowrate,
                                     self.reflux, self.N_feed, self.holdup)

        # CALCULATE COLUMN FLOWS
        # Rectifying section
        Ln = reflux * dist_flowrate
        Vn = Ln + dist_flowrate

        # Stripping section
        Lm = Ln + self.feed_flowrate + self.feed_flowrate * (self.q_feed - 1)
        Vm = Lm - bot_flowrate

        dx_dt = np.zeros_like(x)

        k_vals = self._Inlet.getKeqVLE(pres=self.col_P, temp=temp,
                                       x_liq=x)

        residuals_temp = (x * (k_vals - 1)).sum(axis=1)
        y = k_vals * x

        # Rectifying section
        dx_dt[0] = Vn/M_const * (y[1] - x[0])  # Reflux tank
        dx_dt[1:N_feed - 1] = 1/M_const * (
            Vn * y[2:N_feed] + Ln * x[0:N_feed - 2] -
            Vn * y[1:N_feed - 1] - Ln * x[1:N_feed - 1])

        # Stripping section
        dx_dt[N_feed - 1] = 1/M_const * (
            Vm * y[N_feed] + Ln * x[N_feed - 2] + self.feed_flowrate * z_feed -
            Vn * y[N_feed - 1] - Lm * x[N_feed - 1])  # Feed plate

        dx_dt[N_feed: - 1] = 1/M_const * (
            Vm * y[N_feed + 1:] + Lm * x[N_feed - 1:-2] -
            Vm * y[N_feed: - 1] - Lm*x[N_feed:-1])

        # Reboiler, y_in for reboiler is the same as x_out
        dx_dt[-1] = 1/M_const * (Vm*x[-1] + Lm*x[-2] - Vm*y[-1] - Lm*x[-1])
        mat_bal = np.column_stack((residuals_temp, dx_dt))

        return mat_bal

    def energy_balances(self, time, temp, mole_frac):
        pass
        return

    def solve_unit(self, runtime=None, t0=0):
        # 3 compositions + 1 temperature per plate
        self.len_states = len(self.name_species) + 1

        # init_states = np.column_stack((self.T0, self.x0))

        x_init = self.Liquid_1.mole_frac.copy()
        temp_init = self.Liquid_1.getBubblePoint(pres=self.col_P,
                                                 mole_frac=x_init)

        init_states = np.tile(np.hstack((temp_init, x_init)),
                              (self.num_plates + 1, 1))

        init_derivative = self.material_balances(time=0,
                                                 mole_frac=init_states[:, 1:],
                                                 temp=init_states[:, 0])

        problem = Implicit_Problem(
            self.unit_model, init_states.ravel(), init_derivative.ravel(), t0)

        solver = IDA(problem)
        alg_map = np.zeros_like(init_states)
        alg_map[:, 0] = 1

        solver.algvar = alg_map.ravel()

        time, states, d_states = solver.simulate(runtime)
        self.retrieve_results(time, states)
        return time, states, d_states

    def retrieve_results(self, time, states):
        time = np.asarray(time)
        self.timeProf = time

        indexes = {key: self.states_di[key].get('index', None)
                   for key in self.name_states}

        dp = unpack_discretized(states, self.len_out, self.name_states,
                                indexes=indexes, inputs=None)

        dp['time'] = time
        dp['plate'] = np.arange(1, self.num_plates + 1)

        self.result = DynamicResult(di_states=self.states_di, di_fstates=None,
                                    **dp)

        self.outputs = dp
        # [component_index, time, plate]
        x_comp = np.array(list(dp['mole_frac'].values()))

        # Outlet stream
        path = self.Inlet.path_data
        self.OutletBottom = LiquidStream(
            path, temp=dp['temp'][-1][-1],  # [time, plate]
            mole_frac=x_comp.T[-1][-1],  # [plate, time, component_index]
            mole_flow=self.bot_flowrate)

        self.OutletDistillate = LiquidStream(
            path, temp=dp['temp'][-1][0],  # [time,plate]
            mole_frac=x_comp.T[0][-1],
            mole_flow=self.dist_flowrate)

        self.Outlet = self.OutletBottom

    def plot_profiles(self, times=None, plates=None, pick_comp=None, **fig_kw):
        states = []
        ylab = ['x_liq', 'T']

        if pick_comp is None:
            states.append('mole_frac')
        else:
            states.append(['mole_frac', pick_comp])

        states.append('temp')

        if times is not None:
            marks = cycle(('o', '^', 's', 'd', '+'))
            fig, ax = plot_distrib(self, states, 'plate', times=times,
                                   ylabels=ylab, ncols=2, **fig_kw)

            ind_lines = []
            for ind in range(self.num_species):
                ind_lines.append(ind)
                ind_lines.append(ind + self.num_species)

            for axis in ax:
                if len(axis.lines) > len(times):
                    # shuffle to make markers consistent
                    lines = [axis.lines[ind] for ind in ind_lines]
                else:
                    lines = axis.lines

                for ind, line in enumerate(lines):
                    if ind % len(times) == 0:
                        mark = next(marks)

                    line.set_marker(mark)
                    line.set_markerfacecolor('None')

                axis.yaxis.set_minor_locator(AutoMinorLocator(2))

                axis.xaxis.set_major_locator(MaxNLocator(integer=True))

        elif plates is not None:
            pass  # TODO

        fig.tight_layout()

        return fig, ax
