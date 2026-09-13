# gwrm-gut-event-runner 0.2.0

TODO 3 event-driven test execution platform.

Flow:

1. `gwrm_gut_run_event_driven` starts/reuses one supervised GUT operation.
2. The current Hermes Kanban run is parked with `block_task`.
3. GWRM owns test execution and emits one authenticated terminal callback.
4. `hermes-gut-event-bridge` persists the terminal event and calls `unblock_task`.
5. The dispatcher creates a fresh continuation run.
6. The resumed agent calls `gwrm_gut_collect_event_result(operation_id)` exactly once.

There is no `get_gut_run_status` loop, no `sleep`, and no long-lived LLM wait.

The old `gwrm-gut-runner` 0.1.x remains on disk only as rollback/reference and is removed from active worker/orchestrator profile toolsets by the TODO 3 migration.
