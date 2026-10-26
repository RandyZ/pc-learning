"""Only architecture settings change; never override model, effort or token budgets."""
PROFILES = {
    'A1': {'AGENT_WORKFLOW': 'classic'},
    'B0': {'AGENT_WORKFLOW': 'evidence_v2', 'AGENT_V2_PROFILE': 'serial'},
    'B1': {'AGENT_WORKFLOW': 'evidence_v2', 'AGENT_V2_PROFILE': 'parallel'},
    'B2': {'AGENT_WORKFLOW': 'evidence_v2', 'AGENT_V2_PROFILE': 'parallel_reuse'},
}
V2_REQUIRED = {'AGENT_COMPACT_TOOLS':'true', 'AGENT_REFERENCE_MODE':'false',
    'AGENT_REVIEW_PLAN':'false', 'AGENT_SOLVE_DRAFT':'false', 'AGENT_PARALLEL_QUESTIONS':'false',
    'AGENT_PARALLEL_REVIEW':'false','AGENT_OVERLAP_OCR':'false',
    'AGENT_INCREMENTAL_REVIEW':'false','AGENT_INCREMENTAL_MATH_REVIEW':'false'}


def overrides(name):
    return {**(V2_REQUIRED if name.startswith('B') else {}), **PROFILES[name]}
