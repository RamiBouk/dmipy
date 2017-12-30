from scipy.optimize import brute, minimize
import pkg_resources
from microstruktur.utils.utils import cart2mu
import numpy as np

SPHERES_PATH = pkg_resources.resource_filename(
    'microstruktur', 'data/spheres'
)


class FitBrute2Fine:
    def __init__(self, model, acquisition_scheme, x0_vector, Ns=5, N_sphere_samples=30):
        self.model = model
        self.acquisition_scheme = acquisition_scheme
        self.x0_vector = x0_vector
        self.Ns = Ns

        if x0_vector.squeeze().ndim == 1:
            self.precompute_signal_grid(model, x0_vector, Ns, N_sphere_samples)
        else:
            self.signal_grid = None
            self.parameter_grid = None

    def precompute_signal_grid(self, model, x0_vector, Ns, N_sphere_samples):
        "fixed volume fraction will still be ignored... "

        # import sphere array mu as (theta, phi)
        sphere_vertices = np.loadtxt(
            SPHERES_PATH + "/01-shells-" + str(N_sphere_samples) + ".txt",
            skiprows=1)[:, 1:]
        mu = cart2mu(sphere_vertices)
        grids_per_mu = []
        N_model_fracts = 0
        if len(model.models) > 1:
            N_model_fracts = len(model.models)

        max_cardinality = np.max(model.parameter_cardinality.values())
        for card_counter in range(max_cardinality):
            per_parameter_vectors = []
            counter = 0
            for name, card in model.parameter_cardinality.items()[:-N_model_fracts]:
                par_range = model.parameter_ranges[name]
                if card == 1:
                    if x0_vector[counter] is None:
                        per_parameter_vectors.append(np.linspace(
                            par_range[0], par_range[1], Ns) * model.parameter_scales[name])
                    else:
                        per_parameter_vectors.append([x0_vector[counter]])
                    counter += 1
                if card == 2:
                    if x0_vector[counter] is None:
                        per_parameter_vectors.append(
                            mu[:, card_counter] * model.parameter_scales[name][0])
                    else:
                        per_parameter_vectors.append(
                            [x0_vector[counter + card_counter]])
                    per_parameter_vectors.append([None])
                    counter += 2
            # append nested volume fractions now.
            if N_model_fracts > 0:
                for _ in range(N_model_fracts - 1):
                    per_parameter_vectors.append(np.linspace(0., 1., Ns))
            grids_per_mu.append(np.meshgrid(*per_parameter_vectors))

        counter = 0
        param_dict = {}
        for name, card in model.parameter_cardinality.items()[:-N_model_fracts]:
            if card == 1:
                param_dict[name] = grids_per_mu[0][counter].reshape(-1)
                counter += 1
            if card == 2:
                param_dict[name] = np.concatenate(
                    [grids_per_mu[0][counter][..., None],
                     grids_per_mu[1][counter][..., None]], axis=-1).reshape([-1, 2])
                counter += 2

        # now add nested to regular volume fractions
        if N_model_fracts > 0:
            nested_fractions = grids_per_mu[0][-(N_model_fracts - 1):]
            lin_nested_fractions = [
                fracts.reshape(-1) for fracts in nested_fractions]
            lin_fractions = np.empty(
                (len(lin_nested_fractions[0]), N_model_fracts))
            for i in range(len(lin_nested_fractions[0])):
                lin_fractions[i] = nested_to_normalized_fractions(
                    np.r_[[fract[i] for fract in lin_nested_fractions]])

            counter = 0
            for name, card in model.parameter_cardinality.items()[-N_model_fracts:]:
                param_dict[name] = lin_fractions[:, counter]
                counter += 1

        self.parameter_grid = model.parameters_to_parameter_vector(
            **param_dict)
        self.signal_grid = model.simulate_signal(
            self.acquisition_scheme, self.parameter_grid)

        if N_model_fracts > 0:
            normalized_volume_fractions = self.parameter_grid[:, -N_model_fracts:]
            nested_volume_fractions = normalized_to_nested_fractions_array(
                normalized_volume_fractions)
            self.parameter_grid_nested = np.concatenate(
                [self.parameter_grid[:, :-N_model_fracts],
                 nested_volume_fractions], axis=-1)
        else:
            self.parameter_grid_nested = self.parameter_grid

    def objective_function(self, parameter_vector, data):
        N_fractions = len(self.model.models)
        if N_fractions > 1:
            nested_fractions = parameter_vector[-(N_fractions - 1):]
            normalized_fractions = nested_to_normalized_fractions(
                nested_fractions)
            parameter_vector_ = np.r_[
                parameter_vector[:-(N_fractions - 1)], normalized_fractions]
        else:
            parameter_vector_ = parameter_vector
        parameter_vector_ = parameter_vector_ * self.model.scales_for_optimization
        parameters = {}
        parameters.update(
            self.model.parameter_vector_to_parameters(parameter_vector_)
        )
        E_model = self.model(self.acquisition_scheme, **parameters)
        E_diff = E_model - data
        objective = np.sum(E_diff ** 2) / len(data)
        return objective

    def __call__(self, data, x0_vector):
        N_fractions = len(self.model.models)
        fit_args = (data,)
        bounds = self.model.bounds_for_optimization
        bounds_brute = []
        bounds_fine = list(bounds)
        for i, x0_ in enumerate(x0_vector):
            if x0_ is None:
                bounds_brute.append(
                    slice(bounds[i][0], bounds[i][1],
                          (bounds[i][1] - bounds[i][0]) / float(self.Ns)))
            if x0_ is not None:
                bounds_brute.append(slice(x0_, x0_ + 1e-2, None))
            if (x0_ is not None and
                    self.model.opt_params_for_optimization[i] is False):
                bounds_fine[i] = np.r_[x0_, x0_]

        if N_fractions > 1:  # go to nested bounds
            bounds_brute = bounds_brute[:-1]
            bounds_fine = bounds_fine[:-1]

        if self.signal_grid is not None:
            x0_brute = self.parameter_grid_nested[
                np.argmin(np.sum((self.signal_grid - data) ** 2, axis=-1))]
        else:
            x0_brute = brute(
                self.objective_function, ranges=bounds_brute, args=fit_args,
                finish=None)

        x_fine_nested = minimize(self.objective_function, x0_brute,
                                 args=fit_args, bounds=bounds_fine,
                                 method='L-BFGS-B').x
        if N_fractions > 1:
            nested_fractions = x_fine_nested[-(N_fractions - 1):]
            normalized_fractions = nested_to_normalized_fractions(
                nested_fractions)
            x_fine = np.r_[
                x_fine_nested[:-(N_fractions - 1)], normalized_fractions]
        else:
            x_fine = x_fine_nested
        return x_fine


def nested_to_normalized_fractions(nested_fractions):
    N = len(nested_fractions)
    normalized_fractions = np.zeros(N + 1)
    remaining_fraction = 1.
    for i in range(N):
        normalized_fractions[i] = remaining_fraction * nested_fractions[i]
        remaining_fraction -= normalized_fractions[i]
    normalized_fractions[-1] = remaining_fraction
    return normalized_fractions


def normalized_to_nested_fractions_array(normalized_fractions):
    norm_fracts = np.atleast_2d(normalized_fractions)
    N = norm_fracts.shape[-1]
    nested_fractions = np.zeros(np.r_[norm_fracts.shape[:-1], N - 1])
    remaining_fraction = np.ones(norm_fracts.shape[:-1])
    for i in range(N - 1):
        nested_fractions[..., i] = normalized_fractions[...,
                                                        i] / remaining_fraction
        remaining_fraction -= normalized_fractions[..., i]
    return nested_fractions