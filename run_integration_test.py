import sys
import os
import asyncio

# Ensure src is in path
src_path = os.path.join(os.path.dirname(__file__), "src")
sys.path.insert(0, src_path)

from tests.mflow_cpg.test_integration import test_unified_flow

async def main():
    print("Starting direct integration test run...")
    try:
        await test_unified_flow()
        print("\n=================================")
        print("SUCCESS: Integration test passed!")
        print("=================================")
    except Exception as e:
        print("\n=================================")
        print(f"FAILED: Integration test failed with: {e}")
        print("=================================")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
