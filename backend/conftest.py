# Empty on purpose — its presence tells pytest to treat this directory
# (backend/) as a rootdir, so "from app.main import app" resolves correctly
# in tests/test_health.py.
