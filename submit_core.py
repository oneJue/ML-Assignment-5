import sys
import os
import json
import base64
import hashlib
import importlib
import inspect
import asyncio
import time
import random
import requests
from pathlib import Path
from typing import List, Dict, Tuple
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    os.chdir(BASE_DIR)
except:
    pass

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

load_dotenv()

LEADERBOARD_URL = os.environ.get("LEADERBOARD_URL", "http://101.132.193.95:8000/api/submit")
ASSIGNMENT_ID = os.environ.get("ASSIGNMENT_ID", "05")
TEST_CASES_FILE = "test_cases/test_cases_all_en.json"
NUM_TEST_CASES = 200


def get_student_info():
    info = {
        "student_id": os.environ.get("STUDENT_ID", "").strip(),
        "name": os.environ.get("STUDENT_NAME", "").strip(),
        "nickname": os.environ.get("STUDENT_NICKNAME", "").strip(),
        "main_contributor": os.environ.get("MAIN_CONTRIBUTOR", "").strip().lower()
    }

    missing = [k.upper() for k, v in [
        ("student_id", info["student_id"]),
        ("name", info["name"]),
        ("nickname", info["nickname"]),
        ("main_contributor", info["main_contributor"])
    ] if not v]

    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    if info["main_contributor"] not in ("human", "ai"):
        raise ValueError(f"MAIN_CONTRIBUTOR must be 'human' or 'ai', got: '{info['main_contributor']}'")

    return info


def parse_agent_spec(agent_spec: str) -> Tuple[str, str]:
    if ':' not in agent_spec:
        raise ValueError(f"Invalid agent spec: {agent_spec}. Expected 'module:Class'")
    return agent_spec.split(':', 1)


def collect_agent_files(agent_spec: str) -> Dict[str, bytes]:
    module_name, class_name = parse_agent_spec(agent_spec)
    module = importlib.import_module(module_name)
    getattr(module, class_name)

    files = {}
    visited = set()
    project_root = Path.cwd()

    def is_project_file(file_path: Path) -> bool:
        try:
            path_str = str(file_path.resolve())
            if 'site-packages' in path_str or 'dist-packages' in path_str:
                return False
            file_path.resolve().relative_to(project_root)
            return True
        except:
            return False

    def collect_module(mod):
        if not hasattr(mod, '__name__') or mod.__name__ in visited:
            return
        visited.add(mod.__name__)

        if hasattr(mod, '__file__') and mod.__file__:
            file_path = Path(mod.__file__)
            if file_path.suffix == '.py' and is_project_file(file_path):
                try:
                    with open(file_path, "rb") as f:
                        content = f.read()
                    relative_path = str(file_path.relative_to(project_root))
                    files[relative_path] = content
                except:
                    pass

        try:
            for name in dir(mod):
                if not name.startswith('_'):
                    try:
                        obj = getattr(mod, name)
                        if inspect.ismodule(obj):
                            base_package = module_name.split('.')[0]
                            if hasattr(obj, '__name__') and obj.__name__.startswith(base_package):
                                collect_module(obj)
                    except:
                        pass
        except:
            pass

    collect_module(module)
    return files


def load_local_test_cases(test_cases_file: str, num_cases: int) -> List[Dict]:
    with open(test_cases_file, 'r', encoding='utf-8') as f:
        all_cases = json.load(f)

    if len(all_cases) <= num_cases:
        return all_cases
    return random.sample(all_cases, num_cases)


def run_local_tests(agent_spec: str, test_cases: List[Dict], api_key: str, base_url: str) -> List[Dict]:
    from llm_multi_needle_haystack_tester import LLMMultiNeedleHaystackTester
    from test_case_loader import get_needles
    from evaluators.llm_evaluator import LLMEvaluator

    module_name, class_name = parse_agent_spec(agent_spec)
    module = importlib.import_module(module_name)
    agent_class = getattr(module, class_name)

    results = []
    for idx, test_case in enumerate(test_cases, 1):
        try:
            agent = agent_class(api_key=api_key, base_url=base_url)
            evaluator = LLMEvaluator(api_key=api_key, base_url=base_url)
            needles = get_needles(test_case)

            tester = LLMMultiNeedleHaystackTester(
                model_to_test=agent,
                evaluator=evaluator,
                needles=needles,
                haystack_dir="PaulGrahamEssays",
                question=test_case['question'],
                results_version=1,
                num_tests=1,
                save_results=False,
                save_contexts=False,
                print_ongoing_status=False
            )

            asyncio.run(tester.run_test())
            test_results = tester.get_results()

            if test_results and len(test_results) > 0:
                result = test_results[0]
                results.append({
                    'test_id': test_case.get('id', idx),
                    'score': result['score'],
                    'response': result['model_response'][:200],
                    'duration': result.get('test_duration_seconds', 0)
                })
            else:
                results.append({
                    'test_id': test_case.get('id', idx),
                    'score': 0,
                    'response': 'No results',
                    'duration': 0
                })
        except Exception as e:
            results.append({
                'test_id': test_case.get('id', idx),
                'score': 0,
                'response': f'Error: {str(e)[:100]}',
                'duration': 0
            })

    return results


def submit_to_leaderboard(student_info: Dict, code_files: Dict[str, bytes],
                          results: List[Dict], total_time: float) -> Dict:
    scores = [r['score'] for r in results]

    payload = {
        "student_info": student_info,
        "assignment_id": ASSIGNMENT_ID,
        "metrics": {
            "score": round(sum(scores) / len(scores) if scores else 0, 4),
        },
        "checksums": {
            "evaluate.py": "3n4o5p6a1b2c3d4e5f6g7h8i9j0k1l2m"
        },
        "files": {f: base64.b64encode(c).decode("utf-8") for f, c in code_files.items()},
        "main_contributor": student_info["main_contributor"]
    }
    resp = requests.post(LEADERBOARD_URL, json=payload, timeout=30)
    return resp.json() if resp.status_code == 200 else None


def run_submission(agent_spec: str, api_key: str = None, base_url: str = None) -> int:
    try:
        if not agent_spec:
            raise ValueError("agent_spec is required")

        api_key = api_key or os.environ.get("API_KEY")
        base_url = base_url or os.environ.get("BASE_URL")

        if not api_key or not base_url:
            raise ValueError("API_KEY and BASE_URL are required")

        print("Getting student info...")
        student_info = get_student_info()
        print(f"Student: {student_info['student_id']} - {student_info['name']}")

        print(f"\nCollecting code files for {agent_spec}...")
        code_files = collect_agent_files(agent_spec)
        print(f"Collected {len(code_files)} files")

        print(f"\nLoading test cases from {TEST_CASES_FILE}...")
        test_cases = load_local_test_cases(TEST_CASES_FILE, NUM_TEST_CASES)
        print(f"Loaded {len(test_cases)} test cases")

        print(f"\nRunning tests...")
        start_time = time.time()
        results = run_local_tests(agent_spec, test_cases, api_key, base_url)
        total_time = time.time() - start_time

        scores = [r['score'] for r in results]
        print(f"\nTest Summary:")
        print(f"  Completed: {len(results)}/{len(test_cases)}")
        print(f"  Average: {sum(scores) / len(scores):.2f}/10")
        print(f"  Time: {total_time:.2f}s")

        print(f"\nSubmitting to leaderboard...")
        response = submit_to_leaderboard(student_info, code_files, results, total_time)

        if response:

            print("✓ Submission successful!")
            return 0
        else:
            print(f"\nLeaderboard Response: {response}")
            print("✗ Submission failed")
            return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user")
        return 1
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1