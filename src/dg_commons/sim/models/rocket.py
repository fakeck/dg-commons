import math
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Type, Mapping

import numpy as np
from frozendict import frozendict
from geometry import SE2value, SE2_from_xytheta, SO2_from_angle, SO2value, T2value
from scipy.integrate import solve_ivp
from shapely.affinity import affine_transform
from shapely.geometry import Polygon

from dg_commons.sim import ImpactLocation, IMPACT_EVERYWHERE
from dg_commons.sim.models import ModelType, ModelParameters
from dg_commons.sim.models.model_utils import apply_acceleration_limits, apply_rot_speed_constraint, apply_force_limits, apply_full_ang_vel_limits
from dg_commons.sim.models.rocket_structures import RocketGeometry, RocketParameters
from dg_commons.sim.simulator_structures import SimModel


# todo to review all


@dataclass(unsafe_hash=True, eq=True, order=True)
class RocketCommands:
    F_left: float
    """ Thrust generated by left engine [N]"""
    F_right: float
    """ Thrust generated by right engine [N]"""
    dphi: float
    """ angular velocity of nozzle direction change [rad/s]"""

    idx = frozendict({"F_left": 0, "F_right": 1, "dphi": 2})
    """ Dictionary to get correct values from numpy arrays"""

    @classmethod
    def get_n_commands(cls) -> int:
        return len(cls.idx)

    def __add__(self, other: "RocketCommands") -> "RocketCommands":
        if type(other) == type(self):
            return replace(self, F_left=self.F_left + other.F_left, F_right=self.F_right + other.F_right, dphi=self.dphi + other.dphi)
        else:
            raise NotImplementedError

    __radd__ = __add__

    def __sub__(self, other: "RocketCommands") -> "RocketCommands":
        return self + (other * -1.0)

    def __mul__(self, val: float) -> "RocketCommands":
        return replace(self, F_left=self.F_left * val, F_right=self.F_right * val, dphi=self.dphi * val)

    __rmul__ = __mul__

    def __truediv__(self, val: float) -> "RocketCommands":
        return self * (1 / val)

    def as_ndarray(self) -> np.ndarray:
        return np.array([self.F_left, self.F_right, self.dphi])

    @classmethod
    def from_array(cls, z: np.ndarray):
        assert cls.get_n_commands() == z.size == z.shape[0], f"z vector {z} cannot initialize VehicleInputs."
        return RocketCommands(acc_left=z[cls.idx["acc_left "]], acc_right=z[cls.idx["acc_right"]])


@dataclass(unsafe_hash=True, eq=True, order=True)
class RocketState:
    x: float
    """ CoG x location [m] """
    y: float
    """ CoG y location [m] """
    psi: float
    """ Heading (yaw) [rad] """
    m: float
    """ Mass (dry + fuel) [kg] """
    vx: float
    """ CoG longitudinal velocity [m/s] """
    vy: float
    """ CoG longitudinal velocity [m/s] """
    dpsi: float
    """ Heading (yaw) rate [rad/s] """
    phi: float
    """ Nozzle direction [rad] """
    idx = frozendict({"x": 0, "y": 1, "psi": 2, "m": 3, "vx": 4, "vy": 5, "dpsi": 6, "phi": 7})
    """ Dictionary to get correct values from numpy arrays"""

    @classmethod
    def get_n_states(cls) -> int:
        return len(cls.idx)

    def __add__(self, other: "RocketState") -> "RocketState":
        if type(other) == type(self):
            return replace(
                self,
                x=self.x + other.x,
                y=self.y + other.y,
                psi=self.psi + other.psi,
                m=self.m + other.m,
                vx=self.vx + other.vx,
                vy=self.vy + other.vy,
                dpsi=self.dpsi + other.dpsi,
                phi=self.phi + other.phi,
            )
        else:
            raise NotImplementedError

    __radd__ = __add__

    def __sub__(self, other: "RocketState") -> "RocketState":
        return self + (other * -1.0)

    def __mul__(self, val: float) -> "RocketState":
        return replace(
            self,
            x=self.x * val,
            y=self.y * val,
            psi=self.psi * val,
            m=self.m * val,
            vx=self.vx * val,
            vy=self.vy * val,
            dpsi=self.dpsi * val,
            phi=self.phi * val,
        )

    __rmul__ = __mul__

    def __truediv__(self, val: float) -> "RocketState":
        return self * (1 / val)

    def __repr__(self) -> str:
        return str({k: round(float(v), 2) for k, v in self.__dict__.items() if not k.startswith("idx")})

    def as_ndarray(self) -> np.ndarray:
        return np.array([self.x, self.y, self.psi, self.m, self.vx, self.vy, self.dpsi, self.phi])

    @classmethod
    def from_array(cls, z: np.ndarray):
        assert cls.get_n_states() == z.size == z.shape[0], f"z vector {z} cannot initialize RocketState."
        return RocketState(
            x=z[cls.idx["x"]],
            y=z[cls.idx["y"]],
            psi=z[cls.idx["psi"]],
            m=z[cls.idx["m"]],
            vx=z[cls.idx["vx"]],
            vy=z[cls.idx["vy"]],
            dpsi=z[cls.idx["dpsi"]],
            phi=z[cls.idx["phi"]],
        )


class RocketModel(SimModel[RocketState, RocketCommands]):
    def __init__(self, x0: RocketState, rg: RocketGeometry, rp: RocketParameters):
        self._state: RocketState = x0
        """ Current state of the model"""
        self.XT: Type[RocketState] = type(x0)
        """ State type"""
        self.rg: RocketGeometry = rg
        """ The vehicle's geometry parameters"""
        self.rp: RocketParameters = rp
        """ The vehicle parameters"""

    @classmethod
    def default(cls, x0: RocketState):
        return RocketModel(x0=x0, rg=RocketGeometry.default(), rp=RocketParameters.default())

    def update(self, commands: RocketCommands, dt: Decimal):
        """
        Perform initial value problem integration
        to propagate state using actions for time dt
        """

        def _stateactions_from_array(y: np.ndarray) -> [RocketState, RocketCommands]:
            n_states = self._state.get_n_states()
            state = self._state.from_array(y[0:n_states])
            if self.has_collided:
                actions = RocketCommands(F_left=0, F_right=0, dphi=0)
            else:
                actions = RocketCommands(
                    F_left=y[RocketCommands.idx["F_left"] + n_states],
                    F_right=y[RocketCommands.idx["F_right"] + n_states],
                    dphi=y[RocketCommands.idx["dphi"] + n_states],
                )
            return state, actions

        def _dynamics(t, y):
            state0, actions = _stateactions_from_array(y=y)
            dx = self.dynamics(x0=state0, u=actions)
            du = np.zeros([len(RocketCommands.idx)])
            return np.concatenate([dx.as_ndarray(), du])

        state_np = self._state.as_ndarray()
        action_np = commands.as_ndarray()
        y0 = np.concatenate([state_np, action_np])
        result = solve_ivp(fun=_dynamics, t_span=(0.0, float(dt)), y0=y0)

        if not result.success:
            raise RuntimeError("Failed to integrate ivp!")
        new_state, _ = _stateactions_from_array(result.y[:, -1])
        self._state = new_state
        return

    def dynamics(self, x0: RocketState, u: RocketCommands) -> RocketState:
        """
        Returns state derivative for given control inputs

        Dynamics:
        dx/dt = vx
        dy/dt = vy
        dθ/dt = vθ
        dm/dt = -k_l*(F_l+F_r)
        dvx/dt = 1/m*(sin(phi+θ)*F_l + sin(phi-θ)*F_r)
        dvy/dt = 1/m*(-cos(phi_l+θ)*F_l + cos(phi-θ)*F_r)
        dvθ/dt = 1/I*l2*cos(phi)*(F_r-F_l)
        dphi/dt = vphi
        
        """
        F_lx = apply_force_limits(u.F_left, self.rp)
        F_rx = apply_force_limits(u.F_right, self.rp)
        dphi = apply_full_ang_vel_limits(x0.phi, u.dphi, self.rp)

        
        psi = x0.psi
        dpsi = x0.dpsi
        m = x0.m
        vx = x0.vx
        vy = x0.vy
        phi = x0.phi

        dx = vx
        dy = vy
        dvpsi = dpsi
        dm = -self.rp.C_T*(F_lx+F_rx)
        dvx = 1/m * (math.sin(phi+psi)*F_lx + math.sin(phi-psi)*F_rx)
        dvy = 1/m * (-math.cos(phi+psi)*F_lx + math.cos(phi-psi)*F_rx)
        dvpsi = 1/self.rg.Iz * self.rg.l_m * math.cos(phi) * (F_rx-F_lx)
        dphi = dphi

        return RocketState(x=dx, y=dy, psi=dpsi, m=dm, vx=dvx, vy=dvy, dpsi=dvpsi, phi=dphi)

    def get_footprint(self) -> Polygon:
        """Returns current footprint of the rocket (mainly for collision checking)"""
        footprint = self.rg.outline_as_polygon
        transform = self.get_pose()
        matrix_coeff = transform[0, :2].tolist() + transform[1, :2].tolist() + transform[:2, 2].tolist()
        footprint = affine_transform(footprint, matrix_coeff)
        assert footprint.is_valid
        return footprint

    def get_mesh(self) -> Mapping[ImpactLocation, Polygon]:
        footprint = self.get_footprint()
        impact_locations: Mapping[ImpactLocation, Polygon] = {
            IMPACT_EVERYWHERE: footprint,
        }
        for shape in impact_locations.values():
            assert shape.is_valid
        return impact_locations

    def get_pose(self) -> SE2value:
        return SE2_from_xytheta([self._state.x, self._state.y, self._state.psi])

    @property
    def model_geometry(self) -> RocketGeometry:
        return self.rg

    def get_velocity(self, in_model_frame: bool) -> (T2value, float):
        """Returns velocity at COG"""
        vx = self._state.vx
        vy = self._state.vy
        dpsi = self._state.dpsi
        v_l = np.array([vx, vy])
        if in_model_frame:
            return v_l, dpsi
        rot: SO2value = SO2_from_angle(self._state.psi)
        v_g = rot @ v_l
        return v_g, dpsi

    def set_velocity(self, vel: T2value, dpsi: float, in_model_frame: bool):
        if not in_model_frame:
            rot: SO2value = SO2_from_angle(-self._state.psi)
            vel = rot @ vel
        self._state.vx = vel[0]
        self._state.vy = vel[1]
        self._state.dpsi = dpsi

    @property
    def model_type(self) -> ModelType:
        return self.rg.model_type

    @property
    def model_params(self) -> ModelParameters:
        return self.rp

    def get_extra_collision_friction_acc(self):
        # this model is not dynamic
        pass
