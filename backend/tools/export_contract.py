"""Run from backend: python -m tools.export_contract [--check] [--out DIR]. Deterministic review fixtures.

The generated files live in three different trees (``backend/docs``,
``backend/mocks`` and ``life-circle-demo/src``) and two of them are checked in
with CRLF while the TypeScript ones are LF. An exporter that always wrote LF
therefore turned "one new endpoint" into a whole-file line-ending diff, which is
how a needed OpenAPI update got reverted once already (``evidence/06`` §6.1).

Two rules keep that from happening again:

* **Each file keeps the line ending it already has.** A new file is LF. The bytes
  written are the bytes a reader of this repository would diff against.
* **Nothing here depends on the interpreter that runs it.** FastAPI takes a
  response's default description from ``http.client.responses``, and CPython
  renamed 422's phrase *Unprocessable Entity* → *Unprocessable Content* (3.13),
  so the same routes produced different documents on different Pythons. The
  phrases this spec actually uses are frozen in :data:`FROZEN_PHRASES`.

``--check`` builds every output in memory and compares it with what is on disk,
writing nothing; it exits non-zero when they differ. That is the mode a test or a
reviewer uses to ask "is the committed contract still the one this code
produces?" without also rewriting three unrelated trees as a side effect.
"""
import argparse
import http.client
import json
import sys
import tempfile
from pathlib import Path

from app.checkups.models import (CheckupCapabilities, CheckupFacilities, CheckupIsochrone,
                                 CheckupLayer, CheckupRequest, CheckupSnapshot, CheckupTaskView,
                                 FacilityExtensionDocument, FacilityExtensionRequest,
                                 FacilityExtensionView, FacilityRetryRequest, FacilityRetryView,
                                 FacilityRoute, RetainedCheckupView, RetentionView,
                                 SessionOpenRequest, SessionView, ContinueCheckupRequest)
from app.contracts import (AnalysisResponse, CategoryResult, Data, Facility, FacilityCatalog, Geometry,
                           Issue, Origin, Rules, TaskResultResponse, TaskStatusResponse, OsmOfflineRequest)
from app.hybrid_contracts import HybridRequest, HybridResultResponse, HybridError
from app.rules import DistanceRule
from app.catalog import display_group_keys, keys as catalog_keys, majors as catalog_majors

ANALYSIS_MODELS = (AnalysisResponse, TaskStatusResponse, TaskResultResponse, OsmOfflineRequest,
                   FacilityCatalog, HybridRequest, HybridResultResponse, HybridError)
ANALYSIS_REQUEST_MODELS = (OsmOfflineRequest, HybridRequest)
CHECKUP_MODELS = (CheckupRequest, CheckupTaskView, CheckupSnapshot, CheckupLayer,
                 FacilityRoute, CheckupCapabilities, CheckupFacilities, CheckupIsochrone,
                 FacilityExtensionRequest, FacilityExtensionView, FacilityExtensionDocument,
                 FacilityRetryRequest, FacilityRetryView, SessionOpenRequest, SessionView,
                 RetentionView, RetainedCheckupView, ContinueCheckupRequest)
CHECKUP_REQUEST_MODELS = (CheckupRequest, CheckupFacilities, CheckupIsochrone,
                          FacilityExtensionRequest, FacilityRetryRequest, SessionOpenRequest,
                          ContinueCheckupRequest)

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
EMPTY = Geometry(type="MultiPolygon", coordinates=[])
AREA = Geometry(type="Polygon", coordinates=[[
    [116.39, 39.89], [116.41, 39.89], [116.41, 39.91], [116.39, 39.91], [116.39, 39.89]
]])
ORIGIN = Origin(lng=116.4, lat=39.9)

#: Response descriptions FastAPI derives from the interpreter rather than from the
#: route. Pinned so one document is not "regenerating drift" on another Python.
#: The 422 spelling is RFC 9110's, and it is also what the committed document
#: already says; switching it would hide the real change in a wording diff.
FROZEN_PHRASES = {413: "Content Too Large", 414: "URI Too Long", 422: "Unprocessable Content"}

MOCK_STATUSES = ("complete", "partial", "failed", "empty")


def typescript(models, *, request_models=()):
    def convert(node, request=False):
        if "$ref" in node:
            return node["$ref"].split("/")[-1]
        if "const" in node:
            return json.dumps(node["const"])
        if "enum" in node:
            return " | ".join(json.dumps(v) for v in node["enum"])
        if "anyOf" in node:
            return " | ".join(convert(n) for n in node["anyOf"])
        kind = node.get("type")
        if kind == "array" and "prefixItems" in node:
            return "[" + ", ".join(convert(n) for n in node["prefixItems"]) + "]"
        if kind == "array":
            return f"Array<{convert(node.get('items', {}))}>"
        if kind == "object":
            if "properties" in node:
                return "{\n" + "\n".join(
                    f"  {k}{'?' if (request and k not in node.get('required', [])) or v.get('x-legacy-optional') else ''}: {convert(v)};"
                    for k, v in node["properties"].items()) + "\n}"
            additional = node.get("additionalProperties", {})
            return f"Record<string, {convert(additional) if isinstance(additional, dict) else 'unknown'}>"
        return {"string":"string", "integer":"number", "number":"number", "boolean":"boolean", "null":"null"}.get(kind, "unknown")
    request_names = {model.__name__ for model in request_models}
    definitions = {}
    for model in models:
        schema = model.model_json_schema(mode="validation" if model.__name__ in request_names else "serialization")
        definitions.update(schema.get("$defs", {}))
        definitions[model.__name__] = schema
    text = "// Generated by backend/tools/export_contract.py; response defaults are always serialized.\n" + "\n\n".join(f"export type {name} = {convert(node, name in request_names)};" for name, node in definitions.items()) + "\n"
    major_union = " | ".join(json.dumps(value) for value in catalog_majors())
    minor_union = " | ".join(json.dumps(value) for value in catalog_keys())
    display_group_union = " | ".join(json.dumps(value) for value in display_group_keys())
    text = text.replace(minor_union, 'MinorCategory').replace(major_union, 'MajorCategory')
    text = text.replace(display_group_union, 'DisplayGroup')
    text = text.replace('// Generated by backend/tools/export_contract.py; response defaults are always serialized.\n',
                        '// Generated by backend/tools/export_contract.py; response defaults are always serialized.\n'
                        f'export type MajorCategory = {major_union};\n'
                        f'export type MinorCategory = {minor_union};\n'
                        f'export type DisplayGroup = {display_group_union};\n')
    return text.replace("config?: HybridConfig;", "config?: Partial<HybridConfig>;")


def freeze_status_phrases(spec: dict) -> dict:
    """Replace interpreter-derived response descriptions with the pinned ones.

    Only a description that is exactly the local interpreter's phrase for that
    status is touched: a route that declared its own wording keeps it. Without
    this the same code yields two different documents on Python 3.12 and 3.13.
    """
    for operation in _operations(spec):
        for status, response in operation.get("responses", {}).items():
            if not (status.isdigit() and isinstance(response, dict)):
                continue
            code = int(status)
            if response.get("description") == http.client.responses.get(code):
                pinned = FROZEN_PHRASES.get(code)
                if pinned is not None:
                    response["description"] = pinned
    return spec


def _operations(spec: dict):
    for path in spec.get("paths", {}).values():
        for operation in path.values():
            if isinstance(operation, dict) and "responses" in operation:
                yield operation


def encode(text: str, eol: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", eol).encode("utf-8")


#: 每一份生成物签入时的行尾。行尾是这份文件的一部分，不是生成它的机器的偶然：
#: 钉住它，一次功能改动就不会变成整文件重写，检查模式也才能说出"这份文件的行尾变了"，
#: 而不是默默跟着改（本文件自己被编辑工具写成 LF 时，就没有人说得出来）。
EOL: dict[str, str] = {
    "openapi": "\r\n",
    "analysis-schema": "\r\n",
    "legacy-contract": "\r\n",
    "v2-contract": "\n",
    **{f"mock-{name}": "\r\n" for name in MOCK_STATUSES + ("hybrid-partial",)},
}


def _isolated_settings(scratch: Path):
    """Settings for building the app object, pointed away from the deployment.

    Exporting a contract must not open, migrate or touch the live checkup store
    or the quota ledger: the document is a function of the route table, not of
    this machine's data. ``_env_file=None`` also keeps a deployment's own values
    out of the generated artefacts.
    """
    from app.config import Settings
    return Settings(_env_file=None, checkup_dir=scratch / "checkups",
                    hybrid_ledger_dir=scratch / "hybrid-ledgers",
                    quota_ledger_path=scratch / "quota.sqlite3")


def openapi_spec() -> dict:
    from app.main import create_app
    with tempfile.TemporaryDirectory(prefix="export-contract-") as directory:
        app = create_app(_isolated_settings(Path(directory)))
        try:
            return freeze_status_phrases(app.openapi())
        finally:
            app.state.checkups.store.close()


def _mock_documents() -> dict[str, str]:
    """The legacy review fixtures, as text — identical to what the API serves."""
    documents: dict[str, str] = {}
    from tools.hybrid_fixture import fixture
    documents["hybrid-partial"] = fixture().model_dump_json(indent=2, by_alias=True) + "\n"
    for status in MOCK_STATUSES:
        response = AnalysisResponse(
            analysis_id=f"mock-{status}-v1", generated_at="2026-09-12T00:00:00Z",
            status=status, source="mock", origin=ORIGIN,
            warnings=[Issue(code="MOCK_ONLY", message="手工构造的契约样例，所有设施和规则选择均为虚构。", scope="all")],
        )
        if status in ("complete", "empty"):
            response.rules = Rules(time_confirmation="mock_only", distance=DistanceRule(metric="walking_route", inclusive=True,
                tolerance_m=100, assessment_scope="isochrone", category_policy="major_minor"))
            response.data = Data(geometry=AREA, uncertain_region=EMPTY, unknown_region=EMPTY,
                computation_extent=AREA, facilities=[], blind_points=[], blind_region=EMPTY,
                report="合成样例完成；评估点集为空，不能据设施零结果推导全区盲区。" if status == "empty" else "合成样例完成，起点评估三类服务均有设施。")
            for category in ("market", "pharmacy", "school"):
                index = next(i for i, item in enumerate(response.data.categories) if item.category == category)
                response.data.categories[index] = CategoryResult(category=category, query_status="complete",
                    count_in_circle=0 if status == "empty" else 1,
                    service_status="unknown" if status == "empty" else "covered")
                if status == "complete":
                    response.data.facilities.append(Facility(id=f"mock-{category}", name=f"虚构{category}",
                        category=category, location=ORIGIN, in_circle=True))
            if status == "complete":
                from app.contracts import BlindPoint
                response.data.blind_points = [BlindPoint(location=ORIGIN, category=c, status="covered",
                    evidence="合成规则：该类虚构设施位于评估点，步行距离0m。") for c in ("market", "pharmacy", "school")]
        elif status == "partial":
            response.data.geometry = Geometry(type="Polygon", coordinates=[[
                [116.39,39.89], [116.4,39.89], [116.4,39.91], [116.39,39.91], [116.39,39.89]
            ]])
            response.data.uncertain_region = AREA
            response.data.unknown_region = Geometry(type="Polygon", coordinates=[[
                [116.4,39.89], [116.41,39.89], [116.41,39.91], [116.4,39.91], [116.4,39.89]
            ]])
            response.data.computation_extent = AREA
            response.data.facilities = [Facility(id="mock-pharmacy", name="虚构药店", category="pharmacy", location=ORIGIN, in_circle=True)]
            index = next(i for i, item in enumerate(response.data.categories) if item.category == "pharmacy")
            response.data.categories[index] = CategoryResult(category="pharmacy", query_status="complete", count_in_circle=1, service_status="unknown")
            response.warnings.append(Issue(code="RULES_PENDING", message="距离口径待确认，菜市场和小学数据未知。", scope="rules,categories", severity="pending"))
        else:
            response.errors = [Issue(code="UPSTREAM_UNAVAILABLE", message="合成失败样例：上游暂不可用。", scope="all", severity="error")]
        documents[status] = response.model_dump_json(indent=2) + "\n"
    return documents


def targets(where: Path = REPO) -> dict[str, Path]:
    """Every generated file, keyed by a stable name.

    ``where`` mirrors the repository layout, so ``--out`` produces a full tree
    that can be compared file by file rather than a bag of documents.
    """
    return {
        "openapi": where / "backend/docs/openapi.json",
        "analysis-schema": where / "backend/docs/analysis-response.schema.json",
        "legacy-contract": where / "life-circle-demo/src/api-contract.ts",
        "v2-contract": where / "life-circle-demo/src/checkup/contract.ts",
        **{f"mock-{name}": where / f"backend/mocks/{name}.json"
           for name in MOCK_STATUSES + ("hybrid-partial",)},
    }


def build(where: Path = REPO) -> dict[str, str]:
    """Render every output. Nothing is written and no deployment data is read."""
    documents = {
        "openapi": json.dumps(openapi_spec(), ensure_ascii=False, indent=2) + "\n",
        "analysis-schema": json.dumps(AnalysisResponse.model_json_schema(),
                                      ensure_ascii=False, indent=2) + "\n",
        "legacy-contract": typescript(ANALYSIS_MODELS, request_models=ANALYSIS_REQUEST_MODELS),
        "v2-contract": typescript(CHECKUP_MODELS, request_models=CHECKUP_REQUEST_MODELS),
    }
    for name, text in _mock_documents().items():
        documents[f"mock-{name}"] = text
    missing = set(targets(where)) - set(documents)
    if missing:
        raise RuntimeError(f"no document built for: {sorted(missing)}")
    return documents


def write(where: Path = REPO, documents: dict[str, str] | None = None) -> list[Path]:
    """Write every output, each in its pinned line ending (:data:`EOL`)."""
    documents = documents if documents is not None else build(where)
    written = []
    for name, path in targets(where).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode(documents[name], EOL.get(name, "\n")))
        written.append(path)
    return written


def differences(where: Path = REPO, documents: dict[str, str] | None = None) -> list[str]:
    """Which outputs are not what this code produces.

    The comparison is against the exact bytes :func:`write` would leave, line
    ending included, so "no findings" means running the generator would change
    nothing at all. An ending that drifted is named as such rather than reported
    as a content change: it is a real difference in the file, and it is not a
    contract change, and the two must not be read as the same finding.
    """
    documents = documents if documents is not None else build(where)
    findings = []
    for name, path in targets(where).items():
        eol = EOL.get(name, "\n")
        expected = encode(documents[name], eol)
        if not path.is_file():
            findings.append(f"{path}: missing")
            continue
        actual = path.read_bytes()
        if actual == expected:
            continue
        if actual.replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n"):
            findings.append(f"{path}: line endings differ (expected {'CRLF' if eol == chr(13) + chr(10) else 'LF'})")
        else:
            findings.append(f"{path}: content differs")
    return findings


def export(where: Path = REPO) -> list[Path]:
    """The historical entry point: build and write, in one call."""
    return write(where)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="compare the committed files with the generated ones; write nothing")
    parser.add_argument("--out", type=Path, default=REPO,
                        help="write the mirrored tree under this directory instead of the repository")
    options = parser.parse_args(argv)
    where = options.out.resolve()
    if options.check:
        findings = differences(where)
        if findings:
            print("生成的契约与仓库里的不一致：", file=sys.stderr)
            for finding in findings:
                print(f"  - {finding}", file=sys.stderr)
            print("运行 `python -m tools.export_contract` 重新生成。", file=sys.stderr)
            return 1
        print(f"生成的契约与仓库里的 {len(targets(where))} 份文件一致。")
        return 0
    for path in write(where):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
