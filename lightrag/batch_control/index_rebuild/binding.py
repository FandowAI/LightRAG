"""Bind only uninitialized vectors; retain the original graph/KV/source workspace.

Only an owning worker with a registry-authorized TargetIdentity may call this
before entering its RAG lifespan. This helper grants no activation permission.
See docs/design/BatchIndexRebuild.md for the coordinator contract.
"""

from ..storage import Conflict, IntegrityError
from .builder import TargetIdentity, check_binding
from .projection import META_FIELDS

ATTRIBUTES = {
    "entities": "entities_vdb",
    "relationships": "relationships_vdb",
    "chunks": "chunks_vdb",
}


def initialized(storage):
    return getattr(storage, "_client", None) is not None or bool(
        getattr(storage, "_initialized", False)
    )


def bind_vectors(rag, identity: TargetIdentity):
    """Replace vector instances/factory before initialization; never alter source stores."""
    if getattr(rag, "_index_vector_binding", None) is not None:
        raise Conflict("Vector binding was already selected in this RAG instance")
    if (
        rag.workspace != identity.source.workspace
        or rag.embedding_func.model_name != identity.embedding_model
        or rag.embedding_func.embedding_dim != identity.dimensions
    ):
        raise IntegrityError(
            "Rebuild RAG source workspace or embedding identity differs"
        )
    merge = getattr(rag, "entity_merge", None)
    old = {role: getattr(rag, attr) for role, attr in ATTRIBUTES.items()}
    if merge is not None:
        old["name_entities"] = merge.name_vdb
    if set(old) != set(identity.collections):
        raise IntegrityError("Rebuild RAG auxiliary index binding differs")
    if any(initialized(store) for store in old.values()):
        raise Conflict("Vector bindings cannot change after storage initialization")
    original = rag.vector_db_storage_cls

    def factory(*args, **kwargs):
        if args:
            raise ValueError(
                "Bound vector construction requires explicit keyword fields"
            )
        role = kwargs.get("namespace")
        if role not in identity.collections or kwargs.get("workspace") not in {
            rag.workspace,
            identity.vector_workspace,
        }:
            raise IntegrityError("Vector construction left the selected batch binding")
        embedding = kwargs.get("embedding_func")
        if (
            embedding is None
            or embedding.model_name != identity.embedding_model
            or embedding.embedding_dim != identity.dimensions
        ):
            raise IntegrityError("Vector construction used a different embedding space")
        fields = set(kwargs.get("meta_fields", ()))
        if not META_FIELDS[role] <= fields:
            raise IntegrityError("Vector construction omitted required metadata")
        storage = original(
            **{**kwargs, "workspace": identity.vector_workspace, "meta_fields": fields}
        )
        check_binding(role, storage, identity)
        return storage

    replacement = {
        role: factory(
            namespace=role,
            workspace=rag.workspace,
            embedding_func=rag.embedding_func,
            meta_fields=set(store.meta_fields),
        )
        for role, store in old.items()
    }
    if merge is not None:
        replacement["name_entities"].cosine_better_than_threshold = -1.0
    for role, attr in ATTRIBUTES.items():
        setattr(rag, attr, replacement[role])
    if merge is not None:
        merge.name_vdb = replacement["name_entities"]
    rag.vector_db_storage_cls = factory
    rag._index_vector_binding = identity.model_dump()
    return replacement
