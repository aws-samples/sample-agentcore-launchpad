"""Platform-owned, system-managed agent presets.

A preset is an agent whose *identity and spec are owned by the server*: the ledger row
carries ``Agent.system_key`` (never settable through ``AgentSpec``), its reserved name
cannot be taken by an ordinary agent, and the ordinary lifecycle routes (redeploy,
delete, convert, experiments, canaries) refuse it regardless of the caller's member
permissions. Administrators maintain a preset only through the explicit, idempotent
``/api/system-agents`` install/repair and uninstall routes. Nothing here runs on
startup or on a read.
"""
