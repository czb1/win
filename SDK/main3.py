import sys

from flask import Flask, request, jsonify

sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

app = Flask(__name__)


@app.route('/', methods=['POST'])
def process_request():
    data = request.get_json()
    command = callback(data)
    resp = jsonify(command)
    return resp


def callback(json_data):
    # 选手代码
    res = json_data
    return res


if __name__ == '__main__':
    app.run(port=int(sys.argv[1]))
