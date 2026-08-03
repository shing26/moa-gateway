import pathlib
MAIN = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"

with open(MAIN, 'r', encoding='utf-8-sig') as f:
    c = f.read()

old_startup = '''    global tracer
    try:
        setup_tracing()
        logger.info("opentelemetry tracing enabled")
    except Exception:
        logger.warning("opentelemetry tracing unavailable; using no-op tracer")'''

new_startup = '''    global tracer
    try:
        import os
        from app.observability.tracing import TraceConfig
        cfg = TraceConfig(otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
        setup_tracing(cfg)
    except Exception:
        logger.warning("opentelemetry tracing init failed")'''

c = c.replace(old_startup, new_startup, 1)

with open(MAIN, 'w', encoding='utf-8') as f:
    f.write(c)

import ast
ast.parse(c)
print("OK")
