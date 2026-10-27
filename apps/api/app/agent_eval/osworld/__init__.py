"""OSWorld benchmark adapter: Lumen's own desktop loop driving OSWorld's Ubuntu VM.

The VM runs in Docker under WSL (``env_service.py``, OSWorld's venv) and exposes OSWorld's in-VM HTTP
server. ``OSWorldBody`` implements the desktop body interface against that server, and ``run.py``
creates one real Lumen run per task, drives ``run_vision_loop`` with the body, then asks the env
service for OSWorld's own score.
"""
