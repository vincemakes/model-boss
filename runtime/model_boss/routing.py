"""Pure authority-topology resolution for Model Boss Lite and Max modes."""

from __future__ import annotations

from collections.abc import Mapping

from .models import (
    CandidateTopology,
    CapabilityBand,
    FingerprintEvidenceSource,
    LoadedConfig,
    MainLoop,
    Mode,
    ModelFingerprint,
    PreflightReport,
    Provenance,
    Resolution,
    Role,
    Route,
    RouteProbeResult,
    RunOverrides,
    Status,
    Transport,
    _finalized_resolution,
    _verified_preflight_report,
)

__all__ = (
    "CandidateTopology",
    "PreflightReport",
    "Resolution",
    "RouteProbeResult",
    "canonical_fingerprint",
    "resolve_candidates",
    "preflight_candidates",
    "finalize_resolution",
)


def canonical_fingerprint(
    provider_family: str,
    resolved_model_id: str,
    variant: str = "default",
) -> str:
    """Return the canonical provider/model/variant identity string."""

    return ModelFingerprint(provider_family, resolved_model_id, variant).canonical


def resolve_candidates(
    main: MainLoop | None,
    loaded_config: LoadedConfig,
    run_overrides: RunOverrides,
) -> CandidateTopology:
    """Select configured candidates without making availability claims or I/O."""

    if main is not None and not isinstance(main, MainLoop):
        raise ValueError("main must be a MainLoop or null")
    if not isinstance(loaded_config, LoadedConfig):
        raise ValueError("loaded_config must be LoadedConfig")
    if not isinstance(run_overrides, RunOverrides):
        raise ValueError("run_overrides must be RunOverrides")

    explicit = Provenance("explicit")
    requested_mode = run_overrides.mode or loaded_config.mode
    mode_source = (
        explicit if run_overrides.mode is not None else loaded_config.mode_provenance
    )

    if run_overrides.reviewer is not None:
        reviewer_route_ids = (run_overrides.reviewer,)
        reviewer_source = explicit
    else:
        reviewer_route_ids = loaded_config.preferences.reviewers
        reviewer_source = loaded_config.preference_provenance.reviewers

    if run_overrides.worker is not None:
        worker_route_ids = (run_overrides.worker,)
        worker_source = explicit
    else:
        worker_route_ids = loaded_config.preferences.workers
        worker_source = loaded_config.preference_provenance.workers

    any_explicit_topology = any(
        value is not None
        for value in (
            run_overrides.mode,
            run_overrides.reviewer,
            run_overrides.worker,
        )
    )
    if any_explicit_topology:
        resolution_source = "explicit"
    elif requested_mode is Mode.AUTO and main is not None:
        if main.band is CapabilityBand.BALANCED and reviewer_route_ids:
            resolution_source = reviewer_source.source
        else:
            resolution_source = mode_source.source
    else:
        resolution_source = mode_source.source

    status = Status.OK
    facts: tuple[str, ...] = ()
    if main is None:
        status = Status.NEEDS_CONTEXT
        facts = (
            "main loop canonical identity and capability band are required",
        )

    return CandidateTopology(
        main=main,
        requested_mode=requested_mode,
        routes=loaded_config.routes,
        reviewer_route_ids=reviewer_route_ids,
        worker_route_ids=worker_route_ids,
        mode_source=mode_source,
        reviewer_source=reviewer_source,
        worker_source=worker_source,
        resolution_source=resolution_source,
        status=status,
        facts=facts,
    )


def _validate_probe_mapping(
    route_probe_results: Mapping[str, RouteProbeResult],
) -> dict[str, RouteProbeResult]:
    if not isinstance(route_probe_results, Mapping):
        raise ValueError("route_probe_results must be a mapping")
    probes = dict(route_probe_results)
    for route_id, probe in probes.items():
        if not isinstance(route_id, str) or not isinstance(probe, RouteProbeResult):
            raise ValueError("route probes must map route IDs to RouteProbeResult")
        if route_id != probe.route_id:
            raise ValueError("route probe mapping key must match its route ID")
    return probes


def _transport_is_available(route: Route, probe: RouteProbeResult) -> bool:
    if not probe.reachable or probe.missing_credentials:
        return False
    if route.transport is Transport.EXTERNAL_CLI:
        return probe.executable_available
    return probe.native_agent_available


def _credential_partition_matches(route: Route, probe: RouteProbeResult) -> bool:
    expected = {binding.source_name for binding in route.credential_env}
    reported = set(probe.configured_credentials).union(probe.missing_credentials)
    return reported == expected


def _route_pin_matches(route: Route, fingerprint: ModelFingerprint) -> bool:
    if route.provider_family is None or route.model is None:
        return False
    return (
        route.provider_family.strip().lower()
        == fingerprint.provider_family.strip().lower()
        and route.model.strip().lower()
        == fingerprint.resolved_model_id.strip().lower()
        and (
            route.variant is None
            or route.variant.strip().lower() == fingerprint.variant.strip().lower()
        )
    )


def _reviewer_eligibility(
    candidate: CandidateTopology,
    route_id: str,
    probes: Mapping[str, RouteProbeResult],
) -> tuple[bool, str | None]:
    route = candidate.routes.get(route_id)
    if route is None:
        return False, f"reviewer route {route_id} is not configured"
    if Role.REVIEWER not in route.roles or route.band is not CapabilityBand.AUTHORITY:
        return False, f"reviewer route {route_id} lacks authority reviewer capability"
    if not route.read_only:
        return False, f"reviewer route {route_id} is configured with write capability"
    probe = probes.get(route_id)
    if probe is None:
        return False, f"reviewer route {route_id} has no preflight evidence"
    if not _credential_partition_matches(route, probe):
        return False, f"reviewer route {route_id} has malformed credential evidence"
    if not _transport_is_available(route, probe):
        return False, f"reviewer route {route_id} is unavailable"
    if not probe.reviewer_read_only_enforced:
        return False, f"reviewer route {route_id} is not effectively read-only"
    fingerprint = probe.resolved_fingerprint
    if fingerprint is None or probe.fingerprint_evidence_source is None:
        return False, f"reviewer route {route_id} has no canonical identity evidence"
    if route.transport is Transport.HOST_SUBAGENT:
        if probe.fingerprint_evidence_source is not FingerprintEvidenceSource.HOST_METADATA:
            return False, f"reviewer route {route_id} lacks exact host identity metadata"
    elif probe.fingerprint_evidence_source not in {
        FingerprintEvidenceSource.PINNED_ADAPTER,
        FingerprintEvidenceSource.PROVIDER_RESPONSE,
        FingerprintEvidenceSource.IDENTITY_HANDSHAKE,
    }:
        return False, f"reviewer route {route_id} has invalid external identity evidence"
    if (
        probe.fingerprint_evidence_source
        is FingerprintEvidenceSource.PINNED_ADAPTER
        and route.variant is None
    ):
        return False, f"reviewer route {route_id} lacks an exact pinned variant"
    if route.provider_family is None or route.model is None:
        return False, f"reviewer route {route_id} is not pinned to a canonical model"
    if not _route_pin_matches(route, fingerprint):
        return False, f"reviewer route {route_id} did not match its pinned model"
    if candidate.main is None:
        return False, "main loop canonical identity is unavailable"
    if fingerprint.canonical == candidate.main.fingerprint.canonical:
        return False, f"reviewer route {route_id} resolves to the main-loop model"
    return True, None


def _worker_eligibility(
    candidate: CandidateTopology,
    route_id: str,
    probes: Mapping[str, RouteProbeResult],
) -> tuple[bool, str | None]:
    route = candidate.routes.get(route_id)
    if route is None:
        return False, f"worker route {route_id} is not configured"
    if Role.WORKER not in route.roles or route.read_only:
        return False, f"worker route {route_id} lacks write-capable worker capability"
    probe = probes.get(route_id)
    if probe is None:
        return False, f"worker route {route_id} has no preflight evidence"
    if not _credential_partition_matches(route, probe):
        return False, f"worker route {route_id} has malformed credential evidence"
    if not _transport_is_available(route, probe):
        return False, f"worker route {route_id} is unavailable"
    if route.transport is Transport.EXTERNAL_CLI:
        expected_identity = probe.expected_worker_sandbox_identity
        sandbox_identity = probe.verified_worker_sandbox_identity
        if (
            expected_identity is None
            or sandbox_identity is None
            or sandbox_identity != expected_identity
            or not expected_identity.is_bound_to(route)
            or not sandbox_identity.is_bound_to(route)
        ):
            return False, f"worker route {route_id} has no exact sandbox identity"
    return True, None


def preflight_candidates(
    candidate: CandidateTopology,
    route_probe_results: Mapping[str, RouteProbeResult],
) -> PreflightReport:
    """Apply injected transport evidence and fail closed on authority ambiguity."""

    if not isinstance(candidate, CandidateTopology):
        raise ValueError("candidate must be CandidateTopology")
    probes = _validate_probe_mapping(route_probe_results)
    if candidate.status is not Status.OK:
        return _verified_preflight_report(
            candidate=candidate,
            status=candidate.status,
            resolved_mode=None,
            facts=candidate.facts,
        )

    eligible_reviewers: list[str] = []
    ineligible_reviewers: list[str] = []
    reviewer_facts: list[str] = []
    for route_id in candidate.reviewer_route_ids:
        eligible, fact = _reviewer_eligibility(candidate, route_id, probes)
        (eligible_reviewers if eligible else ineligible_reviewers).append(route_id)
        if fact is not None:
            reviewer_facts.append(fact)

    eligible_workers: list[str] = []
    ineligible_workers: list[str] = []
    worker_facts: list[str] = []
    for route_id in candidate.worker_route_ids:
        eligible, fact = _worker_eligibility(candidate, route_id, probes)
        (eligible_workers if eligible else ineligible_workers).append(route_id)
        if fact is not None:
            worker_facts.append(fact)

    selected_worker = eligible_workers[0] if eligible_workers else None
    requested_mode = candidate.requested_mode
    main = candidate.main
    if requested_mode is Mode.LITE:
        return _verified_preflight_report(
            candidate=candidate,
            status=Status.OK,
            resolved_mode=Mode.LITE,
            selected_worker_route_id=selected_worker,
            eligible_reviewer_route_ids=tuple(eligible_reviewers),
            ineligible_reviewer_route_ids=tuple(ineligible_reviewers),
            eligible_worker_route_ids=tuple(eligible_workers),
            ineligible_worker_route_ids=tuple(ineligible_workers),
            facts=tuple(worker_facts),
        )

    if requested_mode is Mode.AUTO and main is not None:
        if main.band is CapabilityBand.AUTHORITY:
            return _verified_preflight_report(
                candidate=candidate,
                status=Status.OK,
                resolved_mode=Mode.LITE,
                selected_worker_route_id=selected_worker,
                eligible_reviewer_route_ids=tuple(eligible_reviewers),
                ineligible_reviewer_route_ids=tuple(ineligible_reviewers),
                eligible_worker_route_ids=tuple(eligible_workers),
                ineligible_worker_route_ids=tuple(ineligible_workers),
                facts=tuple(worker_facts),
            )
        if main.band is CapabilityBand.FAST:
            return _verified_preflight_report(
                candidate=candidate,
                status=Status.NEEDS_CONTEXT,
                resolved_mode=None,
                eligible_reviewer_route_ids=tuple(eligible_reviewers),
                ineligible_reviewer_route_ids=tuple(ineligible_reviewers),
                eligible_worker_route_ids=tuple(eligible_workers),
                ineligible_worker_route_ids=tuple(ineligible_workers),
                facts=(
                    "fast-band main loop requires an explicit Lite or Max topology",
                    *reviewer_facts,
                    *worker_facts,
                ),
            )

    if not candidate.reviewer_route_ids:
        status = (
            Status.REVIEWER_UNAVAILABLE
            if requested_mode is Mode.MAX
            else Status.NEEDS_CONTEXT
        )
        return _verified_preflight_report(
            candidate=candidate,
            status=status,
            resolved_mode=None,
            eligible_worker_route_ids=tuple(eligible_workers),
            ineligible_worker_route_ids=tuple(ineligible_workers),
            facts=(
                "a distinct authority reviewer route is required",
                *worker_facts,
            ),
        )

    if not eligible_reviewers:
        return _verified_preflight_report(
            candidate=candidate,
            status=Status.REVIEWER_UNAVAILABLE,
            resolved_mode=None,
            ineligible_reviewer_route_ids=tuple(ineligible_reviewers),
            eligible_worker_route_ids=tuple(eligible_workers),
            ineligible_worker_route_ids=tuple(ineligible_workers),
            facts=tuple(reviewer_facts + worker_facts),
        )

    if len(eligible_reviewers) > 1:
        return _verified_preflight_report(
            candidate=candidate,
            status=Status.NEEDS_CONTEXT,
            resolved_mode=None,
            eligible_reviewer_route_ids=tuple(eligible_reviewers),
            ineligible_reviewer_route_ids=tuple(ineligible_reviewers),
            eligible_worker_route_ids=tuple(eligible_workers),
            ineligible_worker_route_ids=tuple(ineligible_workers),
            facts=(
                "multiple eligible reviewer routes remain ambiguous; select one explicitly",
                *reviewer_facts,
                *worker_facts,
            ),
        )

    return _verified_preflight_report(
        candidate=candidate,
        status=Status.OK,
        resolved_mode=Mode.MAX,
        selected_reviewer_route_id=eligible_reviewers[0],
        selected_worker_route_id=selected_worker,
        eligible_reviewer_route_ids=tuple(eligible_reviewers),
        ineligible_reviewer_route_ids=tuple(ineligible_reviewers),
        eligible_worker_route_ids=tuple(eligible_workers),
        ineligible_worker_route_ids=tuple(ineligible_workers),
        facts=tuple(reviewer_facts + worker_facts),
    )


def finalize_resolution(
    candidate: CandidateTopology,
    preflight_report: PreflightReport,
) -> Resolution:
    """Convert a preflight report into a startup topology or blocked result."""

    if not isinstance(candidate, CandidateTopology):
        raise ValueError("candidate must be CandidateTopology")
    if not isinstance(preflight_report, PreflightReport):
        raise ValueError("preflight_report must be PreflightReport")
    if preflight_report.candidate != candidate:
        raise ValueError("preflight report does not belong to this candidate topology")

    if preflight_report.status is not Status.OK:
        return _finalized_resolution(
            status=preflight_report.status,
            candidate=candidate,
            mode=None,
            authority_route_id=None,
            worker="none",
            facts=preflight_report.facts,
            preflight_report=preflight_report,
        )

    mode = preflight_report.resolved_mode
    if mode is None:
        raise ValueError("successful preflight did not resolve a mode")
    authority_route_id = (
        preflight_report.selected_reviewer_route_id if mode is Mode.MAX else None
    )
    if mode is Mode.MAX and authority_route_id is None:
        raise ValueError("Max resolution requires an authority reviewer route")
    if preflight_report.selected_worker_route_id is not None:
        worker = preflight_report.selected_worker_route_id
    elif mode is Mode.MAX:
        worker = "main loop"
    else:
        worker = "none"

    return _finalized_resolution(
        status=Status.OK,
        candidate=candidate,
        mode=mode,
        authority_route_id=authority_route_id,
        worker=worker,
        facts=preflight_report.facts,
        preflight_report=preflight_report,
    )
