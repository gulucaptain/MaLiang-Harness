import json


def dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
