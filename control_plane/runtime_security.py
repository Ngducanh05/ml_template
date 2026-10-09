"""Deterministic runtime scan policy. No network, mutation or reachability inference."""
from collections import Counter
from datetime import datetime, timezone
import json
import re

from .build_contract import validate_request

SEVERITIES = ('CRITICAL', 'HIGH', 'UNKNOWN', 'MEDIUM', 'LOW')
UPSTREAM = 'UPSTREAM_CONTRACT_REMEDIATION_REQUIRED'
RUNTIME = 'RUNTIME_RECIPE_REMEDIATION_REQUIRED'
BASE = 'BASE_RECIPE_REMEDIATION_REQUIRED'
REVIEW = 'WAIVER_REVIEW_REQUIRED'
WAIVER_FIELDS = {'vulnerability_id', 'package_name', 'installed_version', 'build_profile',
                 'reason', 'compensating_controls', 'owner', 'expires_at'}
MATCH_FIELDS = ('vulnerability_id', 'package_name', 'installed_version', 'build_profile')


def load_json(path):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('Duplicate JSON key')
            out[key] = value
        return out
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def blocked_integrity(message):
    return {'decision': 'BLOCK', 'errors': [message], 'counts': {'raw': {}, 'unique_vulnerabilities': 0},
            'blocking_findings': [], 'waived_findings': [], 'non_blocking_findings': [],
            'remediation': {k: [] for k in (UPSTREAM, RUNTIME, BASE, REVIEW)}}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _time(value):
    if not _text(value):
        raise ValueError('Invalid timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Timestamp requires timezone')
    return stamp.astimezone(timezone.utc)


def _waivers(data, now):
    if not isinstance(data, dict) or set(data) != {'schema_version', 'waivers'} or type(data['schema_version']) is not int or data['schema_version'] != 1 or not isinstance(data['waivers'], list):
        raise ValueError('Malformed waiver document')
    entries = {}
    for item in data['waivers']:
        if not isinstance(item, dict) or set(item) != WAIVER_FIELDS or not all(_text(v) for v in item.values()):
            raise ValueError('Malformed waiver entry')
        key = tuple(item[k] for k in MATCH_FIELDS)
        if any(any(c in value for c in '*?[]') for value in key):
            raise ValueError('Wildcard waiver prohibited')
        if key in entries:
            raise ValueError('Duplicate/conflicting waiver')
        if _time(item['expires_at']) <= now:
            raise ValueError('Expired waiver')
        entries[key] = item
    return entries


def evaluate_scan(report, request, waivers, *, now):
    """Evaluate with an explicit aware time; repeated identical inputs give identical output."""
    try:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Evaluation requires timezone-aware time')
        contract, profile = validate_request(request)
        direct = {re.sub(r'[-_.]+', '-', pin.split('==')[0]).lower() for pin in contract.requirements}
        allowed = _waivers(waivers, now)
        if not isinstance(report, dict) or report.get('ArtifactType') != 'container_image':
            raise ValueError('Missing container scan')
        metadata = report.get('Metadata')
        if not isinstance(metadata, dict) or not isinstance(metadata.get('ImageID'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', metadata['ImageID']):
            raise ValueError('Missing image config identity')
        results = report.get('Results')
        if not isinstance(results, list) or not results:
            raise ValueError('Missing scan results')
        findings = []
        for result in results:
            if not isinstance(result, dict) or not all(_text(result.get(k)) for k in ('Target', 'Class', 'Type')):
                raise ValueError('Malformed scan result')
            os_package = result['Class'] == 'os-pkgs' and result['Type'] != 'python-pkg'
            python_package = result['Class'] == 'lang-pkgs' and result['Type'] == 'python-pkg'
            if not os_package and not python_package:
                raise ValueError('Unsupported scan source')
            vulnerabilities = result.get('Vulnerabilities', [])
            if vulnerabilities is None:
                vulnerabilities = []
            if not isinstance(vulnerabilities, list):
                raise ValueError('Malformed vulnerability list')
            for vuln in vulnerabilities:
                if not isinstance(vuln, dict) or not all(_text(vuln.get(k)) for k in ('VulnerabilityID', 'PkgName', 'InstalledVersion', 'Severity')):
                    raise ValueError('Malformed vulnerability record')
                severity = vuln['Severity']
                fixed = vuln.get('FixedVersion')
                if severity not in SEVERITIES or (fixed is not None and not isinstance(fixed, str)):
                    raise ValueError('Malformed severity/fixed version')
                fixed = (fixed or '').strip()
                name = re.sub(r'[-_.]+', '-', vuln['PkgName']).lower()
                owner = BASE if os_package else UPSTREAM if name in direct else RUNTIME
                finding = {'vulnerability_id': vuln['VulnerabilityID'], 'package_name': vuln['PkgName'],
                           'installed_version': vuln['InstalledVersion'], 'fixed_version': fixed,
                           'severity': severity, 'target': result['Target'], 'source': result['Type'],
                           'source_class': result['Class'], 'ownership': owner,
                           'build_profile': profile.build_profile}
                key = tuple(finding[k] for k in MATCH_FIELDS)
                waiver = allowed.get(key)
                waiverable = os_package and severity in ('HIGH', 'UNKNOWN') and not fixed
                if waiver and not waiverable:
                    raise ValueError('Waiver matches a prohibited finding')
                if severity in ('MEDIUM', 'LOW'):
                    finding.update(disposition='NON_BLOCKING', remediation='NON_BLOCKING')
                elif waiver:
                    finding.update(disposition='WAIVED', remediation=REVIEW, waiver=dict(waiver))
                else:
                    finding.update(disposition='BLOCK', remediation=REVIEW if waiverable else owner)
                findings.append(finding)
        findings.sort(key=lambda f: json.dumps(f, sort_keys=True))
        raw = Counter(f['severity'] for f in findings)
        blocking = [f for f in findings if f['disposition'] == 'BLOCK']
        return {'decision': 'BLOCK' if blocking else 'PASS', 'errors': [],
                'image_id': metadata['ImageID'], 'fingerprint': request['fingerprint'],
                'evaluated_at': now.astimezone(timezone.utc).isoformat(),
                'counts': {'raw': {s: raw[s] for s in SEVERITIES},
                           'unique_vulnerabilities': len({f['vulnerability_id'] for f in findings})},
                'blocking_findings': blocking,
                'waived_findings': [f for f in findings if f['disposition'] == 'WAIVED'],
                'non_blocking_findings': [f for f in findings if f['disposition'] == 'NON_BLOCKING'],
                'remediation': {k: [f for f in blocking if f['remediation'] == k] for k in (UPSTREAM, RUNTIME, BASE, REVIEW)}}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return blocked_integrity(str(exc))
