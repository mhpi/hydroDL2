from typing import Dict, Tuple, Union

import torch

from hydroDL2.core.calc import change_param_range
from hydroDL2.core.calc.uh_routing import UH_conv, UH_gamma


class HBVCapillary(torch.nn.Module):
    """Multi-component Pytorch HBV1.1p model with capillary rise modification
    and option to run without warmup.

    Adapted from Farshid Rahmani, Yalan Song.

    Original NumPy version from Beck et al., 2020 (http://www.gloh2o.org/hbv/),
    which runs the HBV-light hydrological model (Seibert, 2005).
    """
    def __init__(self, config=None, device=None):
        super().__init__()
        self.config = config
        self.initialize = False
        self.warm_up = 0
        self.pred_cutoff = 0
        self.warm_up_states = False
        self.static_idx = self.warm_up - 1
        self.dy_params = []
        self.dy_drop = 0.0
        self.variables = ['prcp', 'tmean', 'pet']
        self.routing = False
        self.comprout = False
        self.nearzero = 1e-5
        self.nmul = 1
        self.device = device
        self.parameter_bounds = {
            'parBETA': [1.0, 6.0],
            'parFC': [50, 1000],
            'parK0': [0.05, 0.9],
            'parK1': [0.01, 0.5],
            'parK2': [0.001, 0.2],
            'parLP': [0.2, 1],
            'parPERC': [0, 10],
            'parUZL': [0, 100],
            'parTT': [-2.5, 2.5],
            'parCFMAX': [0.5, 10],
            'parCFR': [0, 0.1],
            'parCWH': [0, 0.2],
            'parBETAET': [0.3, 5],
            'parC': [0, 1]
        }
        self.routing_parameter_bounds = {
            'rout_a': [0, 2.9],
            'rout_b': [0, 6.5]
        }

        if not device:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        if config is not None:
            # Overwrite defaults with config values.
            self.warm_up = config['phy_model']['warm_up']
            self.pred_cutoff = self.warm_up
            self.warm_up_states = config['phy_model']['warm_up_states']
            self.static_idx = config['phy_model']['stat_param_idx']
            self.dy_drop = config['dy_drop']
            self.dy_params = config['phy_model']['dy_params']['HBV_1_1p']
            self.variables = config['phy_model']['forcings']
            self.routing = config['phy_model']['routing']
            self.comprout = config['phy_model'].get('comprout', self.comprout)
            self.nearzero = config['phy_model']['nearzero']
            self.nmul = config['nmul']

        self.set_parameters()

    def set_parameters(self):
        """Get HBV model parameters."""
        phy_params = self.parameter_bounds.keys()
        if self.routing == True:
            rout_params = self.routing_parameter_bounds.keys()
        else:
            rout_params = []
        
        self.all_parameters = list(phy_params) + list(rout_params)
        self.learnable_param_count = len(phy_params) * self.nmul + len(rout_params)

    def unpack_parameters(
            self,
            parameters: torch.Tensor,
            n_steps: int,
            n_grid: int
        ) -> Dict:
        """Extract physics model parameters from NN output.
        
        Parameters
        ----------
        parameters : torch.Tensor
            Unprocessed, learned parameters from a neural network.
        n_steps : int
            Number of time steps in the input data.
        """
        phy_param_count = len(self.parameter_bounds)
        
        # Physical parameters
        phy_params = torch.sigmoid(
            parameters[:, :, :phy_param_count * self.nmul]).view(
                parameters.shape[0],
                parameters.shape[1],
                phy_param_count,
                self.nmul
            )
        # Routing parameters
        if self.routing == True:
            routing_params = torch.sigmoid(
                parameters[-1, :, phy_param_count * self.nmul:]
            )

        # Precompute probability mask for dynamic parameters
        if len(self.dy_params) > 0:
            pmat = torch.ones([n_grid, 1]) * self.dy_drop

        parameter_dict = {}
        for i, name in enumerate(self.all_parameters):
            if i < phy_param_count:
                # Physical parameters
                param = phy_params[self.static_idx, :, i, :]

                if name in self.dy_params:
                    # Make the parameter dynamic
                    drmask = torch.bernoulli(pmat).detach_().to(self.device)
                    dynamic_param = phy_params[:, :, i, :]

                    # Allow chance for dynamic parameter to be static
                    static_param = param.unsqueeze(0).repeat([dynamic_param.shape[0], 1, 1])
                    param = dynamic_param * (1 - drmask) + static_param * drmask
                
                parameter_dict[name] = change_param_range(
                    param=param,
                    bounds=self.parameter_bounds[name]
                )
            elif self.routing:
                # Routing parameters
                parameter_dict[name] = change_param_range(
                    param=routing_params[:, i - phy_param_count],
                    bounds=self.routing_parameter_bounds[name]
                ).repeat(n_steps, 1).unsqueeze(-1)
            else:
                break
        return parameter_dict

    def forward(
            self,
            x_dict: Dict[str, torch.Tensor],
            parameters: torch.Tensor
        ) -> Union[Tuple, Dict[str, torch.Tensor]]:
        """Forward pass for HBV1.1p."""
        # Unpack input data.
        x = x_dict['x_phy']
        muwts = x_dict.get('muwts', None)

        # Initialization
        if not self.warm_up_states:
            # No state warm up - run the full model for warm_up days.
            self.warm_up = 0
        
        if self.warm_up > 0:
            with torch.no_grad():
                x_init = {'x_phy': x[0:self.warm_up, :, :]}
                init_model = HBVCapillary(self.config, device=self.device)

                # Defaults for warm-up.
                init_model.initialize = True
                init_model.warm_up = 0
                init_model.static_idx = self.warm_up-1
                init_model.muwts = None
                init_model.routing = False
                init_model.comprout = False
                init_model.dy_params = []

                Qsinit, SNOWPACK, MELTWATER, SM, SUZ, SLZ = init_model(
                    x_init,
                    parameters
                )
        else:
            # Without warm-up, initialize state variables with zeros.
            n_grid = x.shape[1]
            SNOWPACK = torch.zeros([n_grid, self.nmul],
                                   dtype=torch.float32,
                                   device=self.device) + 0.001
            MELTWATER = torch.zeros([n_grid, self.nmul],
                                    dtype=torch.float32,
                                    device=self.device) + 0.001
            SM = torch.zeros([n_grid, self.nmul],
                             dtype=torch.float32,
                             device=self.device) + 0.001
            SUZ = torch.zeros([n_grid, self.nmul],
                              dtype=torch.float32,
                              device=self.device) + 0.001
            SLZ = torch.zeros([n_grid, self.nmul],
                              dtype=torch.float32,
                              device=self.device) + 0.001

        # Forcings
        P = x[self.warm_up:, :, self.variables.index('prcp')]  # Precipitation
        T = x[self.warm_up:, :, self.variables.index('tmean')]  # Mean air temp
        PET = x[self.warm_up:, :, self.variables.index('pet')] # Potential ET

        # Expand dims to accomodate for nmul models.
        Pm = P.unsqueeze(2).repeat(1, 1, self.nmul)
        Tm = T.unsqueeze(2).repeat(1, 1, self.nmul)
        PETm = PET.unsqueeze(-1).repeat(1, 1, self.nmul)

        n_steps, n_grid = P.size()

        # Parameters
        full_param_dict = self.unpack_parameters(parameters, n_steps, n_grid)

        # Apply correction factor to precipitation
        # P = parPCORR.repeat(n_steps, 1) * P

        # Initialize time series of model variables in shape [time, basins, nmul].
        Qsimmu = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device) + 0.001
        Q0_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device) + 0.0001
        Q1_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device) + 0.0001
        Q2_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device) + 0.0001

        AET = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        recharge_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        excs_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        evapfactor_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        tosoil_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        PERC_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        SWE_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)
        capillary_sim = torch.zeros(Pm.size(), dtype=torch.float32, device=self.device)

        param_dict = full_param_dict.copy()
        for t in range(n_steps):
            # Get dynamic parameter values per timestep.
            for key in self.dy_params:
                param_dict[key] = full_param_dict[key][self.warm_up + t, :, :]

            # Separate precipitation into liquid and solid components.
            PRECIP = Pm[t, :, :]
            RAIN = torch.mul(PRECIP, (Tm[t, :, :] >= param_dict['parTT']).type(torch.float32))
            SNOW = torch.mul(PRECIP, (Tm[t, :, :] < param_dict['parTT']).type(torch.float32))

            # Snow -------------------------------
            SNOWPACK = SNOWPACK + SNOW
            melt = param_dict['parCFMAX'] * (Tm[t, :, :] - param_dict['parTT'])
            # melt[melt < 0.0] = 0.0
            melt = torch.clamp(melt, min=0.0)
            # melt[melt > SNOWPACK] = SNOWPACK[melt > SNOWPACK]
            melt = torch.min(melt, SNOWPACK)
            MELTWATER = MELTWATER + melt
            SNOWPACK = SNOWPACK - melt
            refreezing = param_dict['parCFR'] * param_dict['parCFMAX'] * (
                param_dict['parTT'] - Tm[t, :, :]
                )
            # refreezing[refreezing < 0.0] = 0.0
            # refreezing[refreezing > MELTWATER] = MELTWATER[refreezing > MELTWATER]
            refreezing = torch.clamp(refreezing, min=0.0)
            refreezing = torch.min(refreezing, MELTWATER)
            SNOWPACK = SNOWPACK + refreezing
            MELTWATER = MELTWATER - refreezing
            tosoil = MELTWATER - (param_dict['parCWH'] * SNOWPACK)
            tosoil = torch.clamp(tosoil, min=0.0)
            MELTWATER = MELTWATER - tosoil

            # Soil and evaporation -------------------------------
            soil_wetness = (SM / param_dict['parFC']) ** param_dict['parBETA']
            # soil_wetness[soil_wetness < 0.0] = 0.0
            # soil_wetness[soil_wetness > 1.0] = 1.0
            soil_wetness = torch.clamp(soil_wetness, min=0.0, max=1.0)
            recharge = (RAIN + tosoil) * soil_wetness

            SM = SM + RAIN + tosoil - recharge

            excess = SM - param_dict['parFC']
            excess = torch.clamp(excess, min=0.0)
            SM = SM - excess
            # NOTE: Different from HBV 1.0. Add static/dynamicET shape parameter parBETAET.
            evapfactor = (SM / (param_dict['parLP'] * param_dict['parFC'])) ** param_dict['parBETAET']
            evapfactor = torch.clamp(evapfactor, min=0.0, max=1.0)
            ETact = PETm[t, :, :] * evapfactor
            ETact = torch.min(SM, ETact)
            SM = torch.clamp(SM - ETact, min=self.nearzero)

            # Capillary rise (HBV 1.1p mod) -------------------------------
            capillary = torch.min(SLZ, param_dict['parC'] * SLZ * (1.0 - torch.clamp(SM / param_dict['parFC'], max=1.0)))

            SM = torch.clamp(SM + capillary, min=self.nearzero)
            SLZ = torch.clamp(SLZ - capillary, min=self.nearzero)

            # Groundwater boxes -------------------------------
            SUZ = SUZ + recharge + excess
            PERC = torch.min(SUZ, param_dict['parPERC'])
            SUZ = SUZ - PERC
            Q0 = param_dict['parK0'] * torch.clamp(SUZ - param_dict['parUZL'], min=0.0)
            SUZ = SUZ - Q0
            Q1 = param_dict['parK1'] * SUZ
            SUZ = SUZ - Q1
            SLZ = SLZ + PERC
            Q2 = param_dict['parK2'] * SLZ
            SLZ = SLZ - Q2

            Qsimmu[t, :, :] = Q0 + Q1 + Q2
            Q0_sim[t, :, :] = Q0
            Q1_sim[t, :, :] = Q1
            Q2_sim[t, :, :] = Q2
            AET[t, :, :] = ETact
            SWE_sim[t, :, :] = SNOWPACK
            capillary_sim[t, :, :] = capillary

            recharge_sim[t, :, :] = recharge
            excs_sim[t, :, :] = excess
            evapfactor_sim[t, :, :] = evapfactor
            tosoil_sim[t, :, :] = tosoil
            PERC_sim[t, :, :] = PERC

        # Get the overall average 
        # or weighted average using learned weights.
        if muwts is None:
            Qsimavg = Qsimmu.mean(-1)
        else:
            Qsimavg = (Qsimmu * muwts).sum(-1)

        # Run routing
        if self.routing:
            # Routing for all components or just the average.
            if self.comprout:
                # All components; reshape to [time, gages * num models]
                Qsim = Qsimmu.view(n_steps, n_grid * self.nmul)
            else:
                # Average, then do routing.
                Qsim = Qsimavg

            UH = UH_gamma(param_dict['rout_a'], param_dict['rout_b'], lenF=15)
            rf = torch.unsqueeze(Qsim, -1).permute([1, 2, 0])  # [gages,vars,time]
            UH = UH.permute([1, 2, 0])  # [gages,vars,time]
            Qsrout = UH_conv(rf, UH).permute([2, 0, 1])

            # Routing individually for Q0, Q1, and Q2, all w/ dims [gages,vars,time].
            rf_Q0 = Q0_sim.mean(-1, keepdim=True).permute([1, 2, 0])
            Q0_rout = UH_conv(rf_Q0, UH).permute([2, 0, 1])
            rf_Q1 = Q1_sim.mean(-1, keepdim=True).permute([1, 2, 0])
            Q1_rout = UH_conv(rf_Q1, UH).permute([2, 0, 1])
            rf_Q2 = Q2_sim.mean(-1, keepdim=True).permute([1, 2, 0])
            Q2_rout = UH_conv(rf_Q2, UH).permute([2, 0, 1])

            if self.comprout: 
                # Qs is now shape [time, [gages*num models], vars]
                Qstemp = Qsrout.view(n_steps, n_grid, self.nmul)
                if muwts is None:
                    Qs = Qstemp.mean(-1, keepdim=True)
                else:
                    Qs = (Qstemp * muwts).sum(-1, keepdim=True)
            else:
                Qs = Qsrout

        else:
            # No routing, only output the average of all model sims.
            Qs = torch.unsqueeze(Qsimavg, -1)
            Q0_rout = Q1_rout = Q2_rout = None

        if self.initialize:
            # If initialize is True, it is warm-up mode; only return storages (states).
            return Qs, SNOWPACK, MELTWATER, SM, SUZ, SLZ
        else:
            # Baseflow index (BFI) calculation
            BFI_sim = 100 * (torch.sum(Q2_rout, dim=0) / (
                torch.sum(Qs, dim=0) + self.nearzero))[:,0]

            # Return all sim results.
            out_dict = {
                'flow_sim': Qs,
                'srflow': Q0_rout,
                'ssflow': Q1_rout,
                'gwflow': Q2_rout,
                'AET_hydro': AET.mean(-1, keepdim=True),
                'PET_hydro': PETm.mean(-1, keepdim=True),
                'SWE': SWE_sim.mean(-1, keepdim=True),
                'flow_sim_no_rout': Qsim.unsqueeze(dim=2),
                'srflow_no_rout': Q0_sim.mean(-1, keepdim=True),
                'ssflow_no_rout': Q1_sim.mean(-1, keepdim=True),
                'gwflow_no_rout': Q2_sim.mean(-1, keepdim=True),
                'recharge': recharge_sim.mean(-1, keepdim=True),
                'excs': excs_sim.mean(-1, keepdim=True),
                'evapfactor': evapfactor_sim.mean(-1, keepdim=True),
                'tosoil': tosoil_sim.mean(-1, keepdim=True),
                'percolation': PERC_sim.mean(-1, keepdim=True),
                'capillary': capillary_sim.mean(-1, keepdim=True),
                'BFI_sim': BFI_sim.mean(-1, keepdim=True)   
            }
            
            if not self.warm_up_states:
                for key in out_dict.keys():
                    out_dict[key] = out_dict[key][self.pred_cutoff:, :, :]
            return out_dict
