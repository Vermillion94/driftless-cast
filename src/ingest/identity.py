"""How Driftless Cast identifies itself to upstream data services.

NWS (api.weather.gov) requires a User-Agent with contact information and may
block requests without one; other public APIs ask for the same courtesy. Set
DC_CONTACT to an email or URL that reaches the operator.
"""
import os

CONTACT = os.getenv("DC_CONTACT", "https://driftless-cast.fly.dev")
USER_AGENT = f"DriftlessCast/1.0 (+{CONTACT})"
