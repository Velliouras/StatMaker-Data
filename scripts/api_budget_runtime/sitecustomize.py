"""Loaded before any Python provider client in GitHub Actions."""
import os
if os.getenv('STATMAKER_API_BUDGET_ENFORCE') == 'true':
    try:
        from api_football_budget import install
        install()
    except BaseException:
        # Python normally ignores sitecustomize import failures. Never fail open.
        os.write(2, b'API-Football request guard could not initialize; stopping safely\n')
        os._exit(78)
