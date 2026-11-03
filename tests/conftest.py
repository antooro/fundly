from hypothesis import HealthCheck, settings

# `pytest --hypothesis-profile thorough` runs many more generated cases than the everyday default.
settings.register_profile("default", max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("thorough", max_examples=5000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile("default")
