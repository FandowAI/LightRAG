# Enterprise entity resolution

Production uses `enterprise_identity.py`, `enterprise_knowledge.py`,
`enterprise_merge.py`, and the existing `EntityMergeManager`. See
[EnterpriseKnowledgeMigration.md](EnterpriseKnowledgeMigration.md) for its
source-grounded decisions, persistence and recovery contract.

The local pilot's separate `entity_resolution.py` decorator is intentionally
not installed on top of this manager. Running both could make two identity
judgments before one write, use incompatible caches, and duplicate graph
mutation gates. Verified aliases and department identity are retained through
the existing enterprise migration. Meeting topics and descriptions are not
stable identity scope; they must not split the same company department.
