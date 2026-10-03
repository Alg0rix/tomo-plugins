# Contribute official plugins

Open a pull request with a plugin directory under `plugins/`, its matching
marketplace.json entry, and documentation of tools, pages, dependencies, storage,
and permissions. Use Tomo SDK v1 and synchronous setup(api). Keep user data scoped
with api.user_data_dir and release resources with api.on_dispose.

Run `python -m pip install -r requirements-dev.txt` and
`python scripts/validate.py`. Validate behavior in Tomo before submitting.
No registration in tomo-marketplace is required. Independent community plugins
can instead submit a listing to that repository.
