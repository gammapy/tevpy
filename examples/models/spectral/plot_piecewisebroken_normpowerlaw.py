r"""
.. _piecewise-broken-powerlaw-norm-spectral:

Piecewise Broken Power Law Norm Spectral Model
==============================================

This model parametrises a piecewise broken power law 
with a free norm parameter at each fixed energy node.
"""

# %%
# Example plot
# ------------
# Here is an example plot of the model:

from astropy import units as u
import matplotlib.pyplot as plt
from gammapy.modeling.models import (
    Models,
    SkyModel,
    PiecewiseBrokenPowerLawNormSpectralModel,
)

energy_range = [0.1, 100] * u.TeV
model = PiecewiseBrokenPowerLawNormSpectralModel(
    energy=[0.1, 1, 10, 100] * u.TeV, norms=[1, 3, 5, 2],
)
model.plot(energy_range)
plt.grid(which="both")

# %%
# YAML representation
# -------------------
# Here is an example YAML file using the model:

model = SkyModel(spectral_model=model, name="piecewise-broken-powerlaw-norm-model")
models = Models([model])

print(models.to_yaml())
