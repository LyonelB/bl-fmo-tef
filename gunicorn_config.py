"""Configuration Gunicorn pour BL-FMO-TEF.
IMPORTANT : 1 seul worker (capture HifiBerry = device unique partagé).
"""
import os

bind     = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers  = 1                 # OBLIGATOIRE : un seul capture_engine
worker_class = 'gthread'
threads  = 8                 # concurrence HTTP via threads
timeout  = 120
graceful_timeout = 30
keepalive = 5
loglevel = 'info'
accesslog = '-'
errorlog  = '-'
proc_name = 'bl-fmo-tef'

def post_fork(server, worker):
    """Démarre les threads de fond dans le worker (une seule fois)."""
    from app import start_background
    start_background()
    server.log.info("BL-FMO-TEF : threads de fond lancés via post_fork")
