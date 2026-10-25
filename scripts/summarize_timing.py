"""Read an existing audit/report; never submits or resumes a paid job."""
import argparse
import json
from collections import defaultdict
from pathlib import Path


from math_agent.observability.timing import summarize


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(json.loads(args.report.read_text())), ensure_ascii=False, indent=2))
