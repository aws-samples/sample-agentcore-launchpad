"""``launchpad-policy-interceptor``: the gateway REQUEST / RESPONSE interceptor Lambda of the
per-user principal + Dogwood engine work (stream B; plans/DESIGN-POLICY-USER-PRINCIPAL.md
§13-§15, contract docs/cn/interfaces/policy-principal-contracts.md).

SELF-CONTAINED: stdlib + ``cryptography`` (+ ``boto3`` from the Lambda runtime, imported only
inside ``handler``); nothing under ``app.*`` outside this package is imported, because the
``policy_interceptor`` bootstrap stage zips this directory as is (handler
``interceptor.handler.handler``). ``codec.py`` is a verbatim copy of
``app/policy/contracts/codec.py`` (a test pins the bytes). Inert unless the gateway has the
interceptor attached (``Settings.policy_gateway_interceptor`` + the workspace flag).
"""
