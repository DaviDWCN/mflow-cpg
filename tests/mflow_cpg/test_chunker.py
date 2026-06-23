from __future__ import annotations

import pytest
import os
import tempfile
from typing import AsyncGenerator
from uuid import uuid4
from unittest.mock import patch
from m_flow.data.processing.document_types.Document import Document
from mflow_cpg.chunker import SyntaxAwareCodeChunker

@pytest.mark.anyio
@patch.object(SyntaxAwareCodeChunker, "_get_contextual_retrieval_prefix", return_value="Mocked Contextual Prefix")
async def test_syntax_aware_code_chunker_python(mock_get_prefix):
    code = (
        "class MyClass:\n"
        "    def hello(self):\n"
        "        return 'world'\n"
    )
    
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8") as f:
        f.write(code)
        f_path = f.name

    try:
        doc = Document(
            id=uuid4(),
            name="sample.py",
            processed_path=f_path,
            mime_type="text/x-python"
        )
        
        async def get_text_generator() -> AsyncGenerator[str, None]:
            yield code
            
        chunker = SyntaxAwareCodeChunker(doc, get_text_generator, max_chunk_size=1000)
        chunks = []
        async for chunk in chunker.read():
            chunks.append(chunk)
            
        assert len(chunks) > 0
        # Verify that the chunks contain "# Context: defined in" prefix
        found_context = False
        found_mock_prefix = False
        for chunk in chunks:
            print("Python Chunk Text:", repr(chunk.text))
            if "# Context: defined in" in chunk.text:
                found_context = True
                assert "sample.py" in chunk.text
            if "# Mocked Contextual Prefix" in chunk.text:
                found_mock_prefix = True
        assert found_context
        assert found_mock_prefix
        
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)

@pytest.mark.anyio
@patch.object(SyntaxAwareCodeChunker, "_get_contextual_retrieval_prefix", return_value="Mocked Java Contextual Prefix")
async def test_syntax_aware_code_chunker_java(mock_get_prefix):
    code = (
        "package com.ex;\n"
        "public class MyClass {\n"
        "    public void hello() {}\n"
        "}\n"
    )
    
    with tempfile.NamedTemporaryFile(suffix=".java", delete=False, mode="w", encoding="utf-8") as f:
        f.write(code)
        f_path = f.name

    try:
        doc = Document(
            id=uuid4(),
            name="sample.java",
            processed_path=f_path,
            mime_type="text/x-java"
        )
        
        async def get_text_generator() -> AsyncGenerator[str, None]:
            yield code
            
        chunker = SyntaxAwareCodeChunker(doc, get_text_generator, max_chunk_size=1000)
        chunks = []
        async for chunk in chunker.read():
            chunks.append(chunk)
            
        assert len(chunks) > 0
        # Verify that the chunks contain "// Context: defined in" prefix
        found_context = False
        found_mock_prefix = False
        for chunk in chunks:
            print("Java Chunk Text:", repr(chunk.text))
            if "// Context: defined in" in chunk.text:
                found_context = True
                assert "sample.java" in chunk.text
            if "// Mocked Java Contextual Prefix" in chunk.text:
                found_mock_prefix = True
        assert found_context
        assert found_mock_prefix
        
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)
