"""Strands adapter layer for the Launchpad evaluation engine (N12 E0-5).

A **pure library**: it receives clients and model objects, never builds them, and imports
nothing from ``app.*`` except :mod:`app.evaluation.jobs` (the backend ↔ worker protocol),
so the worker template can inline it byte-for-byte (``__LAUNCHPAD_EVAL_ENGINE_SOURCE__``,
pinned by W-E1's ``test_eval_worker_mirror.py``). Judge models come from
``aws_clients.global_bedrock_model`` on the backend and from the inlined bearer resolver on
the worker — either way the library only ever sees a ``strands.models.Model`` instance.

Modules: ``spans_provider`` (CloudWatch spans → strands ``Session``), ``evaluators``
(Launchpad evaluator ids → strands evaluators), ``results`` (outputs → result records,
run-score aggregation), ``simulator`` (ActorSimulator loop for persona scenarios).
"""
