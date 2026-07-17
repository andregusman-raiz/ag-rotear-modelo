import math
from collections.abc import Mapping as RuntimeMapping
from typing import Mapping, Optional, Tuple

from .contracts import (
    ChildReport,
    EvidenceItem,
    FailureClass,
    TaskRequest,
    ValidationCheck,
    ValidationResult,
)
from .profiles import Profile


class ValidationError(ValueError):
    pass


INDEPENDENT_VERIFIER_CATEGORY = "independent-verifier"
PARTIAL_MUTATION_METRIC = "partial_mutation"
SAFE_SUMMARY_LIMIT = 500


def _fail(path: str, message: str):
    raise ValidationError("%s %s" % (path, message))


def _require_exact(value, expected_type, path: str):
    if type(value) is not expected_type:
        _fail(path, "must be %s" % expected_type.__name__)
    return value


def _copy_mapping(value, path: str) -> Mapping:
    if not isinstance(value, RuntimeMapping):
        _fail(path, "must be a mapping")
    try:
        copied = dict(value)
    except Exception:
        raise ValidationError("%s could not be read" % path) from None
    for key in copied:
        if type(key) is not str or key.strip() == "":
            _fail(path + ".<key>", "must be a non-blank string")
    return copied


def _validate_numeric_metrics(
    value,
    allowed: Tuple[str, ...],
    path: str,
) -> Mapping[str, float]:
    copied = _copy_mapping(value, path)
    allowed_names = frozenset(allowed)
    parsed = {}
    for name, metric in copied.items():
        if name not in allowed_names:
            _fail("%s.%s" % (path, name), "is an unknown metric")
        if type(metric) not in (int, float):
            _fail("%s.%s" % (path, name), "must be a number")
        if not math.isfinite(metric):
            _fail("%s.%s" % (path, name), "must be finite")
        parsed[name] = metric
    return parsed


def _validation_categories(request: TaskRequest) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    if type(request.validation_checks) is not tuple:
        _fail("request.validation_checks", "must be a tuple")
    ids = set()
    categories = set()
    required = []
    declared = []
    for index, check in enumerate(request.validation_checks):
        path = "request.validation_checks[%d]" % index
        _require_exact(check, ValidationCheck, path)
        if type(check.id) is not str or check.id.strip() == "":
            _fail(path + ".id", "must be a non-blank string")
        if type(check.category) is not str or check.category.strip() == "":
            _fail(path + ".category", "must be a non-blank string")
        if type(check.required) is not bool:
            _fail(path + ".required", "must be a boolean")
        if check.id in ids:
            _fail(path + ".id", "is a duplicate validation check id")
        if check.category in categories:
            _fail(path + ".category", "is a duplicate validation check category")
        ids.add(check.id)
        categories.add(check.category)
        declared.append(check.category)
        if check.required:
            required.append(check.category)
    return tuple(declared), tuple(required)


def _safe_evidence(evidence: Tuple[EvidenceItem, ...]) -> Tuple[EvidenceItem, ...]:
    return tuple(
        EvidenceItem(
            item.category,
            item.passed,
            item.summary[:SAFE_SUMMARY_LIMIT],
            item.exit_code,
        )
        for item in evidence
    )


def _evidence_by_category(
    report: ChildReport,
    allowed_categories: Tuple[str, ...],
    path: str,
) -> Mapping[str, EvidenceItem]:
    if type(report.evidence) is not tuple:
        _fail(path + ".evidence", "must be a tuple")
    allowed = frozenset(allowed_categories)
    result = {}
    for index, item in enumerate(report.evidence):
        item_path = "%s.evidence[%d]" % (path, index)
        _require_exact(item, EvidenceItem, item_path)
        if type(item.category) is not str or item.category.strip() == "":
            _fail(item_path + ".category", "must be a non-blank string")
        if type(item.passed) is not bool:
            _fail(item_path + ".passed", "must be a boolean")
        if type(item.summary) is not str or item.summary.strip() == "":
            _fail(item_path + ".summary", "must be a non-blank string")
        if item.exit_code is not None and type(item.exit_code) is not int:
            _fail(item_path + ".exit_code", "must be an integer or null")
        previous = result.get(item.category)
        if previous is not None:
            if previous.passed != item.passed:
                _fail(item_path, "has a conflicting evidence result")
            _fail(item_path, "has a duplicate evidence category")
        if item.category not in allowed:
            _fail(item_path + ".category", "is unknown evidence")
        result[item.category] = item
    return result


def _failed_result(
    failure: FailureClass,
    missing: Tuple[str, ...] = (),
    failed: Tuple[str, ...] = (),
) -> ValidationResult:
    return ValidationResult.failed(failure, missing=missing, failed=failed)


def _blocked_result(
    evidence: Tuple[EvidenceItem, ...],
    failure: FailureClass,
    reason: str,
    metrics=None,
) -> ValidationResult:
    return ValidationResult(
        "blocked",
        _safe_evidence(evidence),
        {} if metrics is None else dict(metrics),
        failure,
        reason,
        False,
    )


def _has_independent_provenance(
    primary_execution_id: Optional[str],
    independent_execution_id: Optional[str],
) -> bool:
    for value in (primary_execution_id, independent_execution_id):
        if type(value) is not str:
            return False
        if value.strip() == "" or value != value.strip():
            return False
    return primary_execution_id != independent_execution_id


def _validate_independent_report(
    profile: Profile,
    report: ChildReport,
    required_categories: Tuple[str, ...],
) -> ValidationResult:
    _require_exact(report, ChildReport, "independent_report")
    allowed = tuple(
        sorted(set(required_categories).union((INDEPENDENT_VERIFIER_CATEGORY,)))
    )
    evidence = _evidence_by_category(report, allowed, "independent_report")
    _validate_numeric_metrics(
        report.metrics,
        profile.progress_metrics,
        "independent_report.metrics",
    )
    marker = evidence.get(INDEPENDENT_VERIFIER_CATEGORY)
    if report.status == "blocked":
        return _blocked_result(
            report.evidence,
            FailureClass.EXTERNAL_BLOCK,
            "blocked",
        )
    if (
        report.status != "pass"
        or report.failure_class is not None
        or marker is None
        or not marker.passed
        or any(not item.passed for item in evidence.values())
    ):
        return _failed_result(
            FailureClass.RISK,
            failed=(INDEPENDENT_VERIFIER_CATEGORY,),
        )
    return ValidationResult.passed(
        _safe_evidence(report.evidence),
        report.metrics,
    )


def _partial_mutation_result(
    profile: Profile,
    report: ChildReport,
    independent_report: Optional[ChildReport],
    required_categories: Tuple[str, ...],
    independent_provenance: bool,
) -> ValidationResult:
    safe_main = _safe_evidence(report.evidence)
    if independent_report is None:
        return ValidationResult(
            "needs-verifier",
            safe_main,
            dict(report.metrics),
            FailureClass.RISK,
            "partial-mutation",
            True,
        )
    if independent_report is report:
        return _blocked_result(
            safe_main,
            FailureClass.RISK,
            "partial-mutation-recovery-invalid",
            report.metrics,
        )
    if not independent_provenance:
        return _blocked_result(
            safe_main,
            FailureClass.RISK,
            "partial-mutation-recovery-invalid",
            report.metrics,
        )
    try:
        verification = _validate_independent_report(
            profile,
            independent_report,
            required_categories,
        )
    except ValidationError:
        return _blocked_result(
            safe_main,
            FailureClass.RISK,
            "partial-mutation-recovery-invalid",
            report.metrics,
        )
    combined = safe_main + _safe_evidence(independent_report.evidence)
    reason = (
        "partial-mutation-recovery-verified"
        if verification.status == "pass"
        else "partial-mutation-recovery-failed"
    )
    return _blocked_result(
        combined,
        FailureClass.RISK,
        reason,
        report.metrics,
    )


def validate_child_report(
    profile: Profile,
    request: TaskRequest,
    report: ChildReport,
    independent_report: Optional[ChildReport] = None,
    primary_execution_id: Optional[str] = None,
    independent_execution_id: Optional[str] = None,
) -> ValidationResult:
    _require_exact(profile, Profile, "profile")
    _require_exact(request, TaskRequest, "request")
    _require_exact(report, ChildReport, "report")
    if report.status not in ("pass", "fail", "blocked"):
        _fail("report.status", "is invalid")
    if report.failure_class is not None and type(report.failure_class) is not FailureClass:
        _fail("report.failure_class", "must be FailureClass or null")
    if request.primary_profile != profile.id:
        _fail("request.primary_profile", "must match profile.id")
    if independent_report is not None and type(independent_report) is not ChildReport:
        _fail("independent_report", "must be ChildReport or null")

    declared, request_required = _validation_categories(request)
    required = tuple(
        sorted(set(profile.validator_categories).union(request_required))
    )
    allowed = tuple(sorted(set(profile.validator_categories).union(declared)))
    if any(
        type(item) is EvidenceItem
        and item.category == INDEPENDENT_VERIFIER_CATEGORY
        for item in report.evidence
    ):
        _fail(
            "main report",
            "cannot contain independent-verifier evidence",
        )
    evidence = _evidence_by_category(report, allowed, "report")
    metrics_allowed = profile.progress_metrics
    if profile.id == "operations":
        metrics_allowed = metrics_allowed + (PARTIAL_MUTATION_METRIC,)
    metrics = _validate_numeric_metrics(
        report.metrics,
        metrics_allowed,
        "report.metrics",
    )

    independent_provenance = (
        independent_report is not None
        and _has_independent_provenance(
            primary_execution_id,
            independent_execution_id,
        )
    )
    partial_mutation = (
        metrics.get(PARTIAL_MUTATION_METRIC, 0.0)
        if profile.id == "operations"
        else 0.0
    )
    if partial_mutation < 0.0:
        _fail(
            "report.metrics.partial_mutation",
            "must be non-negative",
        )
    if partial_mutation > 0.0:
        return _partial_mutation_result(
            profile,
            report,
            independent_report,
            required,
            independent_provenance,
        )

    if report.status == "blocked":
        return _blocked_result(
            report.evidence,
            FailureClass.EXTERNAL_BLOCK,
            "blocked",
        )

    missing = tuple(category for category in required if category not in evidence)
    if missing:
        return _failed_result(FailureClass.COVERAGE, missing=missing)
    failed = tuple(category for category in required if not evidence[category].passed)
    if failed:
        return _failed_result(
            report.failure_class or FailureClass.DEPTH,
            failed=failed,
        )
    if report.status != "pass" or report.failure_class is not None:
        return _failed_result(report.failure_class or FailureClass.DEPTH)

    if request.impact == "critical" and profile.critical_requires_independent_verifier:
        if independent_report is None:
            return ValidationResult.needs_independent_verifier()
        if not independent_provenance:
            return _failed_result(
                FailureClass.RISK,
                failed=("independent-verifier-provenance",),
            )
        verification = _validate_independent_report(
            profile,
            independent_report,
            required,
        )
        if verification.status != "pass":
            return verification
        return ValidationResult.passed(
            _safe_evidence(report.evidence) + verification.evidence,
            metrics,
        )
    if independent_report is not None and not independent_provenance:
        return _failed_result(
            FailureClass.RISK,
            failed=("independent-verifier-provenance",),
        )
    return ValidationResult.passed(_safe_evidence(report.evidence), metrics)
