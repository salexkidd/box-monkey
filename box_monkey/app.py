import io
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime
from urllib.parse import urljoin

from boxsdk import Client, JWTAuth, OAuth2
from boxsdk.config import API
from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_bootstrap import Bootstrap

from flask_executor import Executor
from flask_session import Session
from redis import Redis

UI_ELEMENT_VERSION = "11.0.2"
TITLE = "Box upload same file"

# Flask App Settings
app = Flask(__name__, template_folder="templates")

# Server side session storage
SESSION_TYPE = 'redis'
SESSION_REDIS = Redis(host=os.environ["REDIS_HOST"], port=6379)
app.config.from_object(__name__)

app.config['EXECUTOR_TYPE'] = 'thread'
app.config['EXECUTOR_MAX_WORKERS'] = 4

Session(app)

bootstrap = Bootstrap(app)
executor = Executor(app)


app.config["SECRET_KEY"] = os.environ["FLASK_SECRET_KEY"]

THREE_LEGGED_OAUTH2_REDIRECT_URL = "/oauth-callback"
THREE_LEGGED_OAUTH2_SETTINGS = {
    "client_id": os.environ["BOX_APP_CLIENT_ID"],
    "client_secret": os.environ["BOX_APP_CLIENT_SECRET"],
    "redirect_url": THREE_LEGGED_OAUTH2_REDIRECT_URL,
}


SESSION_KEY_PROCESS_UUID_LIST = "process-uuid-list"
SESSION_KEY_PROCESS_RESULT_DATA = "process-result-data"
SESSION_KEY_BOX_ACCESS_TOKEN = "box-access-token"
SESSION_KEY_BOX_REFRESH_TOKEN = "box-refresh-token"


class For3LeggedOAuth2():
    def __init__(self, client_id, client_secret, redirect_url):
        self._oauth = OAuth2(client_id, client_secret)
        self._redirect_url = urljoin(request.host_url, redirect_url)

    def authorization(self):
        return redirect(self._oauth.get_authorization_url(self._redirect_url)[0])

    def get_access_and_refresh_token(self, code):
        return self._oauth.authenticate(code)


def get_context():
    return {
        "ui_element_version": UI_ELEMENT_VERSION,
        "title": TITLE,
    }


def get_stored_access_token():
    access_token = session.get(SESSION_KEY_BOX_ACCESS_TOKEN, None)
    refresh_token = session.get(SESSION_KEY_BOX_REFRESH_TOKEN, None)
    return access_token, refresh_token


def upload_process(folder_id, access_token, refresh_token):
    oauth_setting = deepcopy(THREE_LEGGED_OAUTH2_SETTINGS)
    del oauth_setting["redirect_url"]
    oauth_setting["access_token"] = access_token
    oauth_setting["refresh_token"] = refresh_token
    box_client = Client(OAuth2(**oauth_setting))

    box_folder = None
    try:
        box_folder = box_client.folder(folder_id).get()
    except Exception as e:
        return {
            "status": "error",
            "msg": "Can't retrieve box folder."
        }

    file_name = "same-file-upload-test-{}.txt".format(datetime.now().isoformat())

    # create newfile
    box_file = None
    try:
        with io.StringIO("1") as f:
            box_file = box_folder.upload_stream(f, file_name)
    except Exception as e:
        return {
            "status": "error",
            "msg": "Can't create new file."
        }

    # and now... upload new version!
    try:
        for i in range(2, 201):
            time.sleep(1)
            with io.StringIO("{}".format(i)) as f:
                box_file.update_contents_with_stream(f)

    except Exception as e:
        return {
            "status": "error",
            "msg": "Can't update  file. trying time: ".format(i),
            "folder_id": box_folder.object_id,
            "file_id": box_file.object_id,
        }

    return {
        "status": "success",
        "msg": "Process Complete",
        "folder_id": box_folder.object_id,
        "file_id": box_file.object_id,
    }


@app.route("/", methods=["GET", ])
def index():
    if session.pop(SESSION_KEY_BOX_ACCESS_TOKEN, None):
        flash(
            "System has purged your box's access token."
            "Please re-authenticate the box to get it again.",
            'warning'
        )
    return render_template("index.html", **get_context())


@app.route("/start-3legged-oauth2", methods=["GET", ])
def start_3legged_oauth2():
    auth = For3LeggedOAuth2(**THREE_LEGGED_OAUTH2_SETTINGS)
    return auth.authorization()


@app.route(THREE_LEGGED_OAUTH2_REDIRECT_URL, methods=["GET", ])
def redirect_url():
    code = request.args.get("code", None)

    if not code:
        return redirect(url_for("index"))

    try:
        auth = For3LeggedOAuth2(**THREE_LEGGED_OAUTH2_SETTINGS)
        access_token, refresh_token = auth.get_access_and_refresh_token(code)
        session[SESSION_KEY_BOX_ACCESS_TOKEN] = access_token
        session[SESSION_KEY_BOX_REFRESH_TOKEN] = refresh_token
    except Exception as e:
        flash(f'Error occured!: {e}', 'danger')
        return redirect(url_for("index"))

    return redirect(url_for("folder_picker"))


@app.route("/folder-picker", methods=["GET", ])
def folder_picker():
    access_token, refresh_token = get_stored_access_token()

    if not access_token:
        return redirect(url_for("index"))

    context = get_context()
    context.update({
        "box_access_token": access_token,
        "refresh_token": refresh_token,
    })
    return render_template("ui-elements.html", **context)


@app.route("/start-upload-process", methods=["POST", ])
def start_upload_process():
    access_token, refresh_token = get_stored_access_token()
    if not access_token:
        return jsonify({'msg':'Box access token does not exist.'}), 401

    folder_id = None
    try:
        folder_id = request.get_json()["folder_id"]
    except Exception as e:
        return Response("folder id does not exist or corrupted json data: {}".format(e)), 400

    process_uuid = str(uuid.uuid4())
    session_list = session.get(SESSION_KEY_PROCESS_UUID_LIST, [])
    session_list.append(process_uuid)
    session[SESSION_KEY_PROCESS_UUID_LIST] = session_list

    # upload_process.submit(folder_id, access_token, refresh_token)
    executor.submit_stored(
        process_uuid,
        upload_process,
        folder_id,
        access_token,
        refresh_token
    )
    return jsonify({'msg':'Accepted', "process-uuid": process_uuid}), 200


@app.route("/process", methods=["GET", ])
def process_list():
    result_list = list()
    process_result_data = session.get(SESSION_KEY_PROCESS_RESULT_DATA, dict())

    for process_uuid in session.get(SESSION_KEY_PROCESS_UUID_LIST, []):
        r = {
            "process-uuid": process_uuid,
            "process_status": "RUNNING",
            "result": None
        }

        if executor.futures.done(process_uuid):
            future = executor.futures.pop(process_uuid)
            process_result_data[process_uuid] = future.result()

        if process_uuid in process_result_data:
            r["result"] = process_result_data[process_uuid]
            r["process_status"] = "COMPLETE"

        result_list.append(r)

    session[SESSION_KEY_PROCESS_RESULT_DATA] = process_result_data

    return jsonify(result_list), 200

@app.route("/process/<process_uuid>", methods=["DELETE", ])
def process_result(process_uuid):

    is_in_process_list = False
    def remove_from_process_list():
        process_list = session.get(SESSION_KEY_PROCESS_UUID_LIST, [])
        process_list.remove(process_uuid)
        session[SESSION_KEY_PROCESS_UUID_LIST] = process_list
        is_in_process_list = True

    def remove_from_result_data():
        process_result_data = session.get(SESSION_KEY_PROCESS_RESULT_DATA, dict())
        del process_result_data[process_uuid]

    def remove_from_futures():
        executor.futures.pop(process_uuid)

    for f in (remove_from_process_list, remove_from_result_data, remove_from_futures):
        try:
            f()
        except Exception as e:
            pass

    if is_in_process_list:
        return "OK", 200
    else:
        return "Not Found", 404


if __name__ == "__main__":
    debug = True
    if os.environ["DEBUG"].upper()  == "FALSE":
        debug = False

    app.run(debug=debug, host='0.0.0.0', port=5000)
