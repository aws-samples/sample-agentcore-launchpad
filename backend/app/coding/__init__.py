"""Remote web coding agent (parent task 10-19-coding-agent).

One platform-owned AgentCore Runtime per workspace runs ``codex app-server`` behind
a small JSON action protocol (``image/``); members' projects are runtime sessions
on it. This package holds the runtime's image source and build
(``image_build``), its role/runtime provisioning (``infra``), the admin
install/uninstall job (``install``), the low-level action call
(``runtime_client``) and the ``/api/coding`` routes (``routers``).
"""
