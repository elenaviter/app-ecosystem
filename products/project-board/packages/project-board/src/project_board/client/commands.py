from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from .journal_operations import JournalIndexWorkflow
from .journals import JournalWorkspace, RepositoryMap, parse_source_repositories
from .outbox_outcomes import submit_assignment_report
from .plan_authority import require_plan_item
from .store import SharedFieldStore


def load_json(path: str) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON object from {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _journal_workspace(args: Any) -> JournalWorkspace:
    repositories = RepositoryMap.from_mapping(
        parse_source_repositories(args.source_repo)
    )
    return JournalWorkspace(args.journal_workspace, repositories)


def execute(args: Any) -> Any:
    command = args.command
    if command == "journal-workspace-init":
        workspace = _journal_workspace(args)
        return workspace.initialize()
    if command == "journal-bind":
        workspace = _journal_workspace(args)
        return workspace.reconcile(
            {
                "project_ref": args.project_ref,
                "journal_home_ref": args.journal_home_ref,
                "project_artifact_ref": args.project_artifact_ref,
                "revision": args.revision,
            },
            create_home=args.create_home,
        )
    if command == "journal-refresh":
        workspace = _journal_workspace(args)
        workspace.rebuild_index()
        return workspace.index_status()
    if command == "project-context":
        return _journal_workspace(args).context(args.project_ref)
    if command == "journal-search":
        workspace = _journal_workspace(args)
        entries = workspace.search(
            args.query,
            project_ref=args.project_ref,
            work_ref=args.work_ref,
            worker_name=args.worker,
            status=args.status,
            limit=args.limit,
        )
        return {"entries": entries, "index": workspace.index_status()}
    if command == "journal-read":
        return _journal_workspace(args).read(args.repository_journal_ref)

    field = SharedFieldStore(args.field)
    if command == "field-init":
        return field.initialize(field_id=args.field_id, title=args.title)
    if command == "project-create":
        return field.create_project(
            project_id=args.project_id,
            title=args.title,
            goal=args.goal,
            owner=args.owner,
        )
    if command == "project-list":
        return field.list_projects()
    if command == "contested-add":
        value = load_json(args.call_file)
        return field.record_contested_call(
            args.project_id,
            actor=args.actor,
            subject=str(value.get("subject") or ""),
            positions=value.get("positions") or [],
            settled_by=str(value.get("settled_by") or ""),
            evidence=str(value.get("evidence") or ""),
            work_ref=str(value.get("work_ref") or ""),
        )
    if command == "contested-list":
        who = str(getattr(args, "who", "") or "").strip()
        if who:
            return field.worker_contested_record(args.project_id, who)
        return field.workers_matrix(args.project_id)
    if command == "assignment-report":
        return submit_assignment_report(
            field,
            args.project_id,
            worker_name=args.worker,
            assignment_ref=args.assignment_ref,
            ownership_version=args.ownership_version,
            state=args.state,
            summary=args.summary,
            result_ref=args.result_ref,
            source_event_ref=args.source_event_ref,
            review_look_at=args.review_look_at,
            review_could_not_verify=args.review_could_not_verify,
            review_reviewer=getattr(args, "reviewer", None),
            review_merged=getattr(args, "merged", None),
            review_deploy=getattr(args, "deploy", None),
            review_nothing_to_deploy=bool(getattr(args, "nothing_to_deploy", False)),
            wait_seconds=args.wait_seconds,
            status_command_prefix=("pb", "worker", "outbox-status"),
        )
    if command == "worker-register":
        return field.register_worker(
            worker_name=args.worker,
            runtime_kind=args.runtime_kind,
            capabilities=args.capability,
            authority_label=args.authority_label,
            host_id=args.host_id,
            host_label=args.host_label,
            host_kind=args.host_kind,
            relay_id=args.relay_id,
            reconcile_ceiling_seconds=args.poll_interval,
        )
    if command == "worker-list":
        return field.list_workers()
    if command == "worker-status":
        return field.set_worker_status(
            args.worker, args.status, actor=args.actor, reason=args.reason
        )
    if command == "mail-send":
        payload = load_json(args.payload_file) if args.payload_file else {}
        body = (
            Path(args.body_file).expanduser().read_text(encoding="utf-8")
            if args.body_file
            else args.body
        )
        resolution = field.resolve_mail_recipient(args.project_id, args.recipient)
        route = str(resolution["route"])
        if args.route != "auto" and args.route != route:
            raise DomainError(
                "field_mail_route_mismatch",
                f"The stable recipient requires the {route} route.",
                status=409,
                details={
                    "recipient": resolution["worker_name"],
                    "required_route": route,
                },
            )
        operation = field.send_mail if route == "local" else field.enqueue_remote_mail
        require_plan_item(
            field,
            project_id=args.project_id,
            worker_name=args.sender,
            work_ref=args.work_ref,
        )
        return operation(
            args.project_id,
            sender=args.sender,
            recipient=resolution["worker_name"],
            kind=args.kind,
            subject=args.subject,
            body=body,
            payload=payload,
            work_ref=args.work_ref,
            correlation_id=args.correlation_id,
            reply_to=args.reply_to,
            idempotency_key=args.idempotency_key,
        )
    if command == "mail-pull":
        return field.pull_mail(
            args.project_id,
            worker_name=args.worker,
            lease_owner=args.lease_owner,
            limit=args.limit,
            lease_seconds=args.lease_seconds,
        )
    if command == "mail-renew":
        return field.renew_mail_lease(
            args.project_id,
            worker_name=args.worker,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=args.lease_owner,
            lease_seconds=args.lease_seconds,
            note=args.note,
        )
    if command == "mail-settle":
        return field.settle_mail(
            args.project_id,
            worker_name=args.worker,
            message_ref=args.message_ref,
            lease_id=args.lease_id,
            lease_owner=args.lease_owner,
            outcome=args.outcome,
            summary=args.summary,
        )
    if command == "journal-index":
        workflow = JournalIndexWorkflow(
            field=field,
            workspace=_journal_workspace(args),
            operation_root=field.control / "operations" / "journal-index",
        )
        return workflow.start(
            project_id=args.project_id,
            worker_name=args.worker,
            repository_journal_ref=args.repository_journal_ref,
        )
    if command == "journal-index-resume":
        workflow = JournalIndexWorkflow(
            field=field,
            workspace=_journal_workspace(args),
            operation_root=field.control / "operations" / "journal-index",
        )
        return workflow.resume(args.operation_id, worker_name=args.worker)
    if command == "journal-index-status":
        workflow = JournalIndexWorkflow(
            field=field,
            workspace=_journal_workspace(args),
            operation_root=field.control / "operations" / "journal-index",
        )
        if args.operation_id:
            return workflow.status(args.operation_id, project_id=args.project_id)
        return workflow.status_by_outbox(
            args.outbox_id,
            project_id=args.project_id,
            repository_journal_ref=args.repository_journal_ref,
        )
    if command == "scope-acquire":
        return field.acquire_scope_lease(
            args.project_id,
            worker_name=args.worker,
            scopes=args.scope,
            ttl_seconds=args.ttl_seconds,
            base_revision=args.base_revision,
        )
    if command == "scope-release":
        return field.release_scope_lease(
            args.project_id,
            lease_ref=args.lease_ref,
            worker_name=args.worker,
            outcome=args.outcome,
        )
    if command == "event-queue":
        metadata = load_json(args.metadata_file) if args.metadata_file else {}
        require_plan_item(
            field,
            project_id=args.project_id,
            worker_name=args.worker,
            work_ref=args.work_ref,
        )
        return field.enqueue_service_event(
            args.project_id,
            worker_name=args.worker,
            kind=args.kind,
            summary=args.summary,
            source_event_ref=args.source_event_ref,
            work_ref=args.work_ref,
            metadata=metadata,
            content_hash_value=args.content_hash,
        )
    raise ValueError(f"unsupported local command: {command}")


__all__ = ["execute", "load_json"]
