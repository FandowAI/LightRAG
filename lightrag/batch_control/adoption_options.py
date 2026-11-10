"""Pin effective provider arguments, including CLI values and role overrides."""

import json

from lightrag.llm.binding_options import OpenAILLMOptions

from .references import no_plaintext_secrets

PROVIDER_FIELDS = {
    item["env_name"]: item["env_name"].removeprefix("OPENAI_LLM_").lower()
    for item in OpenAILLMOptions.args_env_name_type_value()
}
PROVIDER_ENVIRONMENT = {
    prefix + name
    for prefix in ("", "QUERY_", "EXTRACT_", "KEYWORD_", "VLM_")
    for name in PROVIDER_FIELDS
}

RAG_ENVIRONMENT = {
    "ENTITY_EXTRACTION_USE_JSON": "entity_extraction_use_json",
    "MAX_GLEANING": "entity_extract_max_gleaning",
    "MAX_EXTRACTION_RECORDS": "entity_extract_max_records",
    "MAX_EXTRACTION_ENTITIES": "entity_extract_max_entities",
    "FORCE_LLM_SUMMARY_ON_MERGE": "force_llm_summary_on_merge",
    "SUMMARY_MAX_TOKENS": "summary_max_tokens",
    "SUMMARY_CONTEXT_SIZE": "summary_context_size",
    "SUMMARY_LENGTH_RECOMMENDED": "summary_length_recommended",
    "SUMMARY_LANGUAGE": "_resolved_summary_language",
    "ENABLE_LLM_CACHE": "enable_llm_cache",
    "ENABLE_LLM_CACHE_FOR_EXTRACT": "enable_llm_cache_for_entity_extract",
    "EMBEDDING_CHUNK_OVERLAP_TOKEN_SIZE": "embedding_chunk_overlap_token_size",
    "VLM_PROCESS_ENABLE": "vlm_process_enable",
}


def provider_environment(args, roles):
    result = {}
    if args.llm_binding != "openai":
        return result
    values = {"": OpenAILLMOptions.options_dict(args)}
    for role, config in roles.items():
        if config.get("binding", "openai") != "openai":
            continue
        options = config.get("metadata", {}).get("provider_options")
        if options is None:
            options = OpenAILLMOptions.options_dict_for_role(args, role)
        values[role.upper() + "_"] = options
    for prefix, options in values.items():
        no_plaintext_secrets(options)
        if set(options) - set(PROVIDER_FIELDS.values()):
            raise ValueError("Source has provider arguments without a startup binding")
        for environment, field in PROVIDER_FIELDS.items():
            value = options.get(field)
            if value is not None:
                result[prefix + environment] = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, allow_nan=False)
                )
    return result
