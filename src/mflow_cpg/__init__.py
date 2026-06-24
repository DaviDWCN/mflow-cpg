from m_flow.shared.config_registry import register_config_provider as reg_mflow_config
from omnicpg.orchestrator.pipeline import register_config_provider as reg_omni_config
from omnicpg.orchestrator.pipeline import register_pipeline_post_processor
from mflow_cpg.config import get_config, ConfigManager

# Register config providers to core layers
reg_mflow_config(get_config)
reg_omni_config(get_config)


# Register semantic enrichment engine to omnicpg pipeline as a post-processor
def run_semantic_enrichment(adapter, project_id):
    cfg = get_config()
    if cfg.semantic_analysis.enabled:
        from mflow_cpg.semantic_engine import SemanticEnrichmentEngine
        import logging

        logger = logging.getLogger(__name__)
        logger.info("Running unified LLM semantic enrichment engine (post-processor)")
        engine = SemanticEnrichmentEngine(adapter, cfg)
        engine.enrich_project(project_id)


register_pipeline_post_processor(run_semantic_enrichment)

# Auto-register unified retriever to m_flow community registry
try:
    import mflow_cpg.retriever
except ImportError:
    pass

__all__ = ["get_config", "ConfigManager"]
