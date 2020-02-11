#
#  Copyright (c) 2019-2020, ETH Zurich. All rights reserved.
#
#  Please, refer to the LICENSE file in the root directory.
#  SPDX-License-Identifier: BSD-3-Clause
#
from flask import Flask, request, jsonify
import pickle

# task states
import async_task
import os
import logging
from logging.handlers import TimedRotatingFileHandler
from cscs_api_common import check_header, get_username
import tasks_persistence as persistence

AUTH_HEADER_NAME = 'Authorization'

STATUS_IP   = os.environ.get("STATUS_IP")
STORAGE_IP  = os.environ.get("STORAGE_IP")
COMPUTE_IP     = os.environ.get("COMPUTE_IP")
KONG_URL    = os.environ.get("KONG_URL")

TASKS_PORT    = os.environ.get("TASKS_PORT", 5000)

# redis info:
PERSISTENCE_IP   = os.environ.get("PERSISTENCE_IP")
PERSIST_PORT = os.environ.get("PERSIST_PORT")
PERSIST_PWD  = os.environ.get("PERSIST_PWD")

# expire time in seconds, for squeue or sacct tasks:
TASK_EXP_TIME = 300

debug = os.environ.get("DEBUG_MODE", None)

# task dict, key is the task_id
tasks = {}

app = Flask(__name__)

# redis connection object
r = None

def init_queue():

    global r

    r=persistence.create_connection(host=PERSISTENCE_IP, port=PERSIST_PORT, passwd=PERSIST_PWD, db=0)

    if r == None:
        app.logger.error("Persistence Database is not functional")
        app.logger.error("Tasks microservice cannot be started")
        persistence.close_connection(r)
        return

    # dictionary: [task_id] = {hash_id,status_code,user,data}
    task_list = persistence.get_all_tasks(r)
    # last_task_id = persistence.get_last_task_id(r)

    # key = task_id ; values = {status_code,user,data}
    for id, value in task_list.items():
        # task_list has id with format task_id, ie: task_2
        # therefore it must be splitted by "_" char:
        task_id = id.split("_")[1]

        status  = value["status"]
        user    = value["user"]
        data    = value["data"]

        t = async_task.AsyncTask(task_id,user)
        t.set_status(status,data)
        tasks[t.hash_id] = t

    persistence.close_connection(r)


@app.route("/",methods=["GET"])
def list_tasks():
    # checks if AUTH_HEADER_NAME is set
    try:
        auth_header = request.headers[AUTH_HEADER_NAME]
    except KeyError as e:
        app.logger.error("No Auth Header given")
        return jsonify(description="No Auth Header given"), 401

    if not check_header(auth_header):
        return jsonify(description="Invalid header"), 401

    # getting username from auth_header
    username = get_username(auth_header)

    user_tasks = {}

    #iterate over tasks getting user tasks
    for task_id,task in tasks.items():
        if task.user == username:
            user_tasks[task_id] = task.get_status()


    data = jsonify(tasks=user_tasks)
    return data, 200

# create a new task, response should be task_id of created task
@app.route("/",methods=["POST"])
def create_task():
    # remote address request by Flask
    remote_addr= request.remote_addr

    if debug:
        logging.info('debug: tasks: create_task: remote_address: ' + remote_addr)

    # checks if request comes from allowed microservices
    if remote_addr not in [COMPUTE_IP, STORAGE_IP]:
        return jsonify(description="Invalid request address"), 403

    # checks if request has service header
    try:
        service = request.headers["X-Firecrest-Service"]

        if service not in ["storage","compute"]:
            return jsonify(description="Service {} is unknown".format(service)), 403

    except KeyError:
        return jsonify(description="No service informed"), 403




    auth_header = request.headers[AUTH_HEADER_NAME]
    if not check_header(auth_header):
        return jsonify(description="Invalid header"), 401

    # getting username from auth_header
    username=get_username(auth_header)

    # if "last_task_id" key isn't in persistence server
    # creates it

    # QueuePersistence connection
    global r

    if not persistence.create_last_task_id(r):
        persistence.close_connection(r)
        return jsonify(description="Couldn't create task"), 400

    # incremente the last_task_id in 1 unit
    task_id = persistence.incr_last_task_id(r)

    # if there was a problem getting new task_id from persistence
    if task_id == None:
        persistence.close_connection(r)
        return jsonify(description="Couldn't create task"), 400

    # create task with service included
    t = async_task.AsyncTask(task_id=str(task_id), user=username, service=service)
    tasks[t.hash_id] = t

    persistence.save_task(r,id=task_id,task=t.get_status())
    persistence.close_connection(r)

    # {"id":task_id,
    #               "status":async_task.QUEUED,
    #               "msg":async_task.status_codes[async_task.QUEUED]}

    app.logger.info("New task created: {hash_id}".format(hash_id=t.hash_id))
    app.logger.info(t.get_status())
    task_url = "{KONG_URL}/tasks/{hash_id}".format(KONG_URL=KONG_URL,hash_id=t.hash_id)

    data = jsonify(hash_id=t.hash_id, task_url=task_url)

    return data, 201



# should return status of the task
@app.route("/<id>",methods=["GET"])
def get_task(id):
    # checks if AUTH_HEADER_NAME is set
    try:
        auth_header = request.headers[AUTH_HEADER_NAME]
    except KeyError as e:
        app.logger.error("No Auth Header given")
        return jsonify(description="No Auth Header given"), 401

    if not check_header(auth_header):
        return jsonify(description="Invalid header"), 401

    # getting username from auth_header
    username = get_username(auth_header)

    # for better knowledge of what this id is
    hash_id = id

    try:
        if not tasks[hash_id].is_owner(username):
            return jsonify(description="Operation not permitted. Invalid task owner."), 403

        task_status=tasks[hash_id].get_status()
        task_status["task_url"]="{KONG_URL}/tasks/{hash_id}".format(KONG_URL=KONG_URL, hash_id=hash_id)
        data = jsonify(task=task_status)
        return data, 200

    except KeyError:
        data = jsonify(error="Task {id} does not exist".format(id=id))
        return  data, 404


# update status of the task with task_id = id
@app.route("/<id>",methods=["PUT"])
def update_task(id):

    # remote address request by Flask
    remote_addr = request.remote_addr

    # checks if request comes from allowed microservices
    if remote_addr not in [COMPUTE_IP, STORAGE_IP]:
        return jsonify(description="Invalid request address"), 403

    # checking data from JSON, need to do before check if auth_header
    # is needed (for download from SWIFT to filesystem).
    if request.is_json:

        try:
            data = request.get_json(force=True)
            app.logger.info(data)
            status=data["status"]
            msg=data["msg"]
        except Exception as e:
            app.logger.error(type(e))

    else:

        try:
            msg  = request.form["msg"]
            app.logger.info(msg)
        except Exception as e:
            msg = None
            # app.logger.error(e.message)

        status = request.form["status"]


    # if status is not download from SWIFT failed (ST_DWN_ERR) or finished (ST_DWN_END)
    # or if upload from filesystem to SWIFT failed (ST_UPL_ERR) or finished (ST_UPL_END)
    # then check of header is needed. ***
    # owner_needed is True if is not ***
    owner_needed = False
    if status not in [async_task.ST_DWN_END , async_task.ST_DWN_ERR, async_task.ST_UPL_ERR, async_task.ST_UPL_END] :


        #introduced in order to download from SWIFT to
        auth_header = request.headers[AUTH_HEADER_NAME]

        if not check_header(auth_header):
            return jsonify(description="Invalid header"), 401

        # getting username from auth_header
        owner_needed = True
        username = get_username(auth_header)

    # for better knowledge of what this id is
    hash_id = id

    # if username isn't taks owner, then deny access, unless is ***
    try:
        if owner_needed and not tasks[hash_id].is_owner(username):
            return jsonify(description="Operation not permitted. Invalid task owner."), 403
    except KeyError:
        data = jsonify(error="Task {hash_id} does not exist".format(hash_id=hash_id))
        return data, 404


    # app.logger.info("Status {status}. Msg {msg}".format(status=status,msg=msg))

    # checks if status request is valid:
    if status not in async_task.status_codes:
        data = jsonify(error="Status code error",status=status)
        app.logger.error(data)
        return data, 400


    # if no msg on request, default status msg:
    if msg == None:
        app.logger.info(msg)
        msg = async_task.status_codes[status]
        app.logger.info(msg)

    app.logger.info(msg)
    # update task in memory
    tasks[hash_id].set_status(status=status, data=msg)

    global r
    #update task in persistence server
    if not persistence.save_task(r, id=tasks[hash_id].task_id, task=tasks[hash_id].get_status()):
        persistence.close_connection(r)
        app.logger.error("Error saving task")
        app.logger.error(tasks[hash_id].get_status())
        return jsonify(description="Couldn't update task"), 400

    persistence.close_connection(r)
    app.logger.info("New status for task {hash_id}: {status}".format(hash_id=hash_id,status=status))

    data = jsonify(success="task updated")
    return data, 200



@app.route("/<id>",methods=["DELETE"])
def delete_task(id):
    # checks if AUTH_HEADER_NAME is set
    try:
        auth_header = request.headers[AUTH_HEADER_NAME]
    except KeyError as e:
        app.logger.error("No Auth Header given")
        return jsonify(description="No Auth Header given"), 401

    if not check_header(auth_header):
        return jsonify(description="Invalid header"), 401

    # remote address request by Flask
    remote_addr = request.remote_addr

    # checks if request comes from allowed microservices
    if remote_addr not in [COMPUTE_IP, STORAGE_IP]:
        return jsonify(description="Invalid request address"), 403

    # getting username from auth_header
    username = get_username(auth_header)

    # for better knowledge of what this id is
    hash_id = id

    # if username isn't taks owner, then deny access
    try:
        if not tasks[hash_id].is_owner(username):
            return jsonify(description="Operation not permitted. Invalid task owner."), 403
    except KeyError:
        data = jsonify(error="Task {id} does not exist".format(id=id))
        return data, 404

    try:
        global r

        if not persistence.del_task(r,id=tasks[hash_id].task_id):
            persistence.close_connection(r)
            return jsonify(error="Failed to delete task on persistence server"), 400

        persistence.close_connection(r)
        data = jsonify(success="task deleted")
        tasks[hash_id].set_status(status=async_task.DELETED, data="")
        return data, 204

    except Exception:
        data = jsonify(Error="Failed to delete task")
        return data, 400

#set expiration for task, in case of Jobs list or account info:
@app.route("/task-expire/<id>",methods=["POST"])
def expire_task(id):
    # checks if AUTH_HEADER_NAME is set
    try:
        auth_header = request.headers[AUTH_HEADER_NAME]
    except KeyError as e:
        app.logger.error("No Auth Header given")
        return jsonify(description="No Auth Header given"), 401

    if not check_header(auth_header):
        return jsonify(description="Invalid header"), 401

    # remote address request by Flask
    remote_addr = request.remote_addr

    # checks if request comes from allowed microservices
    if remote_addr not in [COMPUTE_IP, STORAGE_IP]:
        return jsonify(description="Invalid request address"), 403

    # getting username from auth_header
    username = get_username(auth_header)

    # for better knowledge of what this id is
    hash_id = id

    # if username isn't taks owner, then deny access
    try:
        if not tasks[hash_id].is_owner(username):
            return jsonify(description="Operation not permitted. Invalid task owner."), 403
    except KeyError:
        data = jsonify(error="Task {id} does not exist".format(id=id))
        return data, 404

    try:
        global r

        app.logger.info("id: {id}".format(id=tasks[hash_id].task_id))
        if not persistence.set_expire_task(r,id=tasks[hash_id].task_id,secs=TASK_EXP_TIME):
            persistence.close_connection(r)
            return jsonify(error="Failed to set expiration time on task in persistence server"), 400

        persistence.close_connection(r)
        data = jsonify(success="Task expiration time set to {exp_time} secs.".format(exp_time=TASK_EXP_TIME))
        # tasks[hash_id].set_status(status=async_task.EXPIRED)

        return data, 200

    except Exception:
        data = jsonify(Error="Failed to delete task")
        return data, 400


# get status for status microservice
# only used by STATUS_URL otherwise forbidden

@app.route("/status",methods=["GET"])
def status():

    app.logger.info("Test status of service")

    if request.remote_addr != STATUS_IP:
        app.logger.warning("Invalid remote address: {addr}".format(addr=request.remote_addr))
        return jsonify(error="Invalid access"), 403

    return jsonify(success="ack"), 200


# entry point for storage service to recompile
# tasks when initialize service
# only used by STORAGE_URL otherwise forbidden

@app.route("/taskslist",methods=["GET"])
def tasklist():

    global r

    app.logger.info("Get Storage service tasks")
    app.logger.info("STORAGE_IP is {storage_ip}".format(storage_ip=STORAGE_IP))

    # reject if not STORAGE_IP remote address
    if request.remote_addr != STORAGE_IP:
        app.logger.warning("Invalid remote address: {addr}".format(addr=request.remote_addr))
        return jsonify(error="Invalid access"), 403


    storage_tasks = persistence.get_service_tasks(r, "storage")
    persistence.close_connection(r)

    # app.logger.info(storage_tasks)
    if storage_tasks == None:
        return jsonify(error="Persistence server task retrieve error"), 404

    return jsonify(tasks=storage_tasks), 200



if __name__ == "__main__":
    # log handler definition
    # timed rotation: 1 (interval) rotation per day (when="D")
    logHandler = TimedRotatingFileHandler('/var/log/tasks.log', when='D', interval=1)

    logFormatter = logging.Formatter('%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
                                     '%Y-%m-%d:%H:%M:%S')
    logHandler.setFormatter(logFormatter)
    logHandler.setLevel(logging.DEBUG)

    # get app log (Flask+werkzeug+python)
    logger = logging.getLogger()

    # set handler to logger
    logger.addHandler(logHandler)

    init_queue()

    # set to debug = False, so stderr and stdout go to log file
    app.run(debug=debug, host='0.0.0.0', use_reloader=False, port=TASKS_PORT)