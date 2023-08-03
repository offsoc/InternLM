import argparse
import ast
import copy
import os
import socket
import multiprocessing
import threading
import time
import pickle
import portpicker
import re
import shutil
import eventlet
import subprocess

from enum import Enum, unique
from flask_socketio import SocketIO
from loguru import logger
from flask import Flask, request
from prettytable import PrettyTable
from datetime import datetime
from waitress import serve
from typing import Dict,Set
from enum import Enum
from subprocess import PIPE, STDOUT, Popen
from threading import Thread
from datetime import datetime


@unique
class LockContextType(Enum):
    """
    Enum to express the type of the lock
    """

    THREAD_LOCK = 1
    PROCESS_LOCK = 2


_LOCK_TYPE_MAPPING = {
    LockContextType.THREAD_LOCK: threading.Lock,
    LockContextType.PROCESS_LOCK: multiprocessing.Lock,
}


class LockContext(object):
    """
    Overview:
        Generate a LockContext in order to make sure the thread safety.
    Interfaces:
        __init__, __enter__, __exit__
    Example:
        >>> with LockContext() as lock:
        >>>     print("Do something here.")
    """

    def __init__(self, type_: LockContextType = LockContextType.THREAD_LOCK):
        r"""
        Overview:
            init the lock according to given type
        """
        self.lock = _LOCK_TYPE_MAPPING[type_]()

    def acquire(self):
        self.lock.acquire()

    def release(self):
        self.lock.release()

    def __enter__(self):
        """
        Overview:
            Entering the context and acquire lock
        """
        self.lock.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Overview:
            Quiting the context and release lock
        """
        self.lock.release()


def now_time():
    return datetime.now().strftime("%b%d_%H-%M-%S")

class RestartInfo:
    """RestartInfo"""

    def __init__(self) -> None:
        """
        If a task restarts many times and still fails, give up restarting
        """
        self.job_infos = []
        self.restart_count = 0
        self.latest_restart_time = time.time()


class RankInfo:
    """RankInfo"""

    def __init__(self, rank, hostname, device_id) -> None:
        """
        rank level information

        Args:
            rank (int):
            hostname (str):
            device_id (int):
        """
        # self.jobInfo = None
        self.rank = rank
        self.hostname = hostname
        self.device_id = device_id
        self.last_report_time = time.time()
        self.exception_list = []

    def __str__(self) -> str:
        return f"rank:{self.rank}, host:{self.hostname}"



class JobInfo:
    """JobInfo"""

    def __init__(self, world_size, slurm_jobname, slurm_jobid, script_config) -> None:
        """job-level information

        Args:
            world_size (int):
            slurm_jobname (str):
            slurm_jobid (int):
            script_config (str):
        """
        self.world_size = world_size
        self.slurm_jobname = slurm_jobname
        self.slurm_jobid = slurm_jobid
        self.script_config = script_config
        self.restart_count = 0
        self.rank_map: Dict[str, RankInfo] = dict()  # [rank -> RankInfo]
        self.job_state = JobState.INIT
        self.nodelist: Set[str] = set()
        self.is_hunman_scancel = False
        self.last_ckpt = None
        # self.wait_count = 0 # If an exception is found, we wait for _max_waiting_seconds seconds,
        # expecting to wait for the error message of all processes, and then decide whether to restart

    def __str__(self) -> str:
        return f"Jobname:{self.slurm_jobname}, jobID:{self.slurm_jobid}, world_size:{self.world_size}"


class MessageLevel(Enum):
    ERROR = 0
    FATAL = 1
    IGNORE = 2
    COMPLETE = 3


class JobState(Enum):
    INIT = 0
    RUN = 1
    COMPLETE = 2
    ERROR = 3
    ABORT = 4


def handle_scitofloat(config:str):
    pattern = r'([+-]?\d+(\.\d+)?)([Ee])([+-]?\d+)'
    sci_nums_list = re.findall(pattern, config)
    new_config = copy.deepcopy(config)
    print(sci_nums_list)
    for itr in sci_nums_list:
        float_num = float(itr[0]) * (10 ** int(itr[3]))
        float_num =  f'{float_num:.20f}'
        orig_num = f"{itr[0]}{itr[2]}{itr[3]}"
        new_config = new_config.replace(orig_num, float_num)
    
    return new_config


def get_node_hostnames(nodelist:str):
    hostnames = []

    tmp_nodelist = copy.deepcopy(nodelist)
    tmp_nodelist = tmp_nodelist.replace(" ", "")
    tmp_nodelist = re.sub(r"[\[\]]", "", tmp_nodelist)
    

    tmplist = tmp_nodelist.split(",")
    pattern1 = r"^\d+$"
    pattern2 = r"^\d+-\d+$"
    prefix = "-".join(tmplist[0].split("-")[:-1]) + "-"
    
    for tmpiter in tmplist:
        if re.match(pattern1, tmpiter):
            hostnames.append(prefix + tmpiter)
        elif re.match(pattern2, tmpiter):
            begin, end = int(tmpiter.split("-")[0]), int(tmpiter.split("-")[1])
            hostnames.extend([prefix+str(i) for i in range(begin, end+1)])
        else:
            prefix = "-".join(tmpiter.split("-")[:-1]) + "-"
            hostnames.append(tmpiter)
            
    return hostnames


def exec_cmd(cmd_with_args: list, shell = False, env=None) -> str:
    results = ""
    with Popen(cmd_with_args, shell=shell, stdout=PIPE, stderr=STDOUT, env=env) as output:
        for line in iter(output.stdout.readline, b""):
            results += line.rstrip().decode() + "\n"

    return results

def do_find_slow_node(timeout: int, nodelist: str):
    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_script = os.path.join(current_dir, "nccl_test.sh")
    
    # handle nodelist
    nodes=get_node_hostnames(nodelist)
    
    cmd = f"sh {test_script} {nodes}"
    with Popen(cmd, shell=True, stdout=PIPE, stderr=STDOUT) as p:
        try:
            outs, errs = p.communicate(timeout=timeout)
            print(outs.decode())
            if errs:
                print(errs.decode())
        except subprocess.TimeoutExpired as e:
            p.kill()
    
    exclude_nodestr = ""
    if os.path.exists("exclude_nodes.log"):
        with open("exclude_nodes.log", "r", encoding="utf-8") as f:
            exclude_nodestr = f.read().strip()
            exclude_nodestr = exclude_nodestr.replace("\n", "")
    
    return exclude_nodestr



def get_slurm_jobinfo(jobid):
    sacct_cmd = (
                f'sacct -j {jobid} --format="JobID%100, JobName%100, UID%20,'
                " User%30, State%20, QuotaType%20, ExitCode%10, Cluster%20,"
                " VirtualPartition%30, Partition%30, AllocCPUS%10, AllocGPUS%10,"
                ' AllocNodes%10, NodeList%255, NTasks%30"'
            )

    res= exec_cmd(sacct_cmd, shell=True)
    tmp = res.splitlines()


    job_info = {}
    job_info["jobid"] = tmp[2][:100]
    job_info["jobname"] = tmp[2][100:201]
    job_info["uid"] = tmp[2][201:222]
    job_info["user"] = tmp[2][222:253]
    job_info["state"] = tmp[2][253:274]
    job_info["quotatype"] = tmp[2][274:295]
    job_info["exitcode"] = tmp[2][295:306]
    job_info["cluster"] = tmp[2][306:327]
    job_info["virtual_partition"] = tmp[2][327:358]
    job_info["partition"] = tmp[2][358:389]
    job_info["alloc_cpus"] = tmp[2][389:400]
    job_info["alloc_gpus"] = tmp[2][400:411]
    job_info["alloc_nodes"] = tmp[2][411:422]
    job_info["nodelist"] = tmp[2][422:678]
    job_info["ntasks"] = tmp[-1][678:709]

    for key in job_info:
        if key == "state":
            job_info[key] = job_info[key].strip().split(" ")[0]
        value = job_info[key].replace(" ", "")
        if value == "Unknown":
            value = None
        job_info[key] = value

    return job_info

def scancel_slurm_job(job_id: str, env=None):
    """
    scancel current slurm job.
    """

    # scancel jobid
    scancel_cmd = ["scancel", f"{job_id}"]
    logger.info(scancel_cmd)
    exec_cmd(scancel_cmd, env)


def sbatch_slurm_job(job_info: dict, script_cfg: str, exclude_nodes: str, env=None):
    """
    submit a slurm sbatch job.
    return True if submit the job successfully, False if failed.
    """

    config=str(script_cfg)
    
    logger.info("sbatch job")
    logger.info(config)
    
    run_cmd = f'srun python train.py --config "{config}" --auto_restart --launcher "slurm" '
    
    jobname=job_info['jobname']
    
    ntasks=job_info["ntasks"]
    nodes=job_info["alloc_nodes"]
    cpus_per_task=int(int(job_info["alloc_cpus"]) / int(ntasks))
    gpus_per_task=int(int(job_info["alloc_gpus"]) / int(ntasks))
    partition=job_info["virtual_partition"]
    
    sbatch_filepath = f"sbatch_{jobname}.slurm"
    with open(sbatch_filepath, "w") as f:
        lines = [
            "#!/bin/bash\n",
            f"#SBATCH --partition={partition}\n",
            f"#SBATCH --job-name={jobname}\n",
            f"#SBATCH --ntasks={ntasks}\n",
            f"#SBATCH --nodes={nodes}\n",
            f"#SBATCH --cpus-per-task={cpus_per_task}\n",
            f"#SBATCH --gpus-per-task={gpus_per_task}\n",
            f"#SBATCH --output={jobname}_{now_time()}.log\n",
            ]
        if exclude_nodes != "":
            lines.append(f"#SBATCH  --exclude={exclude_nodes}")
        lines.extend([ "\n",
            run_cmd,
            "\n"])
        
        f.writelines(lines)
    
    
    sbatch_cmd=f"sbatch {sbatch_filepath}"
    
    if env is not None:
        sbatch_env = copy.deepcopy(env)
    else:
        sbatch_env = copy.deepcopy(os.environ)
    
    for key in sbatch_env:
        if "slurm" in key.lower():
            sbatch_env.pop(key)

    logger.info(sbatch_cmd)

    results = exec_cmd(sbatch_cmd, sbatch_env)
    logger.info(results)

    if "Submitted batch job" not in results:
        return False, f'submit sbatch job "{sbatch_cmd}" failed, please check it.'

    new_jobid = re.search(r'\b(\d+)\b', results).group(1)  # get new jobid
    return True, new_jobid



def determine_job_is_alive(slurm_job_id: str):
    jobinfo = get_slurm_jobinfo(slurm_job_id)
    curjob_state = jobinfo["state"]
    
    if curjob_state not in ["RUNNING", "PENDING"] :
        return False
    return True

def now_time():
    return datetime.now().strftime("%b%d_%H-%M-%S")

class Coordinator(object):
    """Coordinator"""

    def __init__(self, ipaddr: str, port: str, nccl_test: bool, nccl_timeout: int = None) -> None:
        """init

        Args:
            ipaddr (str): coordinator ip
            port (str): coordinator port
        """
        self._ip = ipaddr
        self._port = port
        self.is_nccl_test = nccl_test
        self.nccl_timeout = nccl_timeout
        self._lock = LockContext(type_=LockContextType.THREAD_LOCK)
        self._timeout = 1200
        self.jobname_map: Dict[str, JobInfo] = dict()  # job_name -> JobInfo
        self._job_map: Dict[str, JobInfo] = dict()  # slurm_jobID -> JobInfo
        self._polling_thread = Thread(target=self.main_thread)
        self.stopped = False
        self.last_activity = time.time()
        self._max_waiting_seconds = 15
        self.restarting = False
        self.json_preifx = "./json_logs"
        self.fired_error = set()
        # Try to read the state of the Coordinator before the crash from the local persistent file
        self.init_from_json()
        self._polling_thread.start()  # start polling
        self.restart_job_info: Dict[str, RestartInfo] = dict()  #
        logger.info(f"pwd: {os.getcwd()}", flush=True)
        # self._rank_map : dict[str, dict[str, RankInfo]] = dict()    # slurm_jobID -> [rank -> RankInfo]

    def reset_job_state(self, job_id: str):
        # Reset information about slurm id and rank in job_info.
        job_info = self.jobname_map[self._job_map[job_id].slurm_jobname]
        job_info.rank_map = dict()
        job_info.slurm_jobid = 0
        # Delete the mapping of job_id --> job_info in job_map
        del self._job_map[job_id]

    def init_from_json(self):
        try:
            os.makedirs(self.json_preifx)
        except FileExistsError:
            pass
        for _, _, files in os.walk(self.json_preifx):
            for name in files:
                pf = os.path.join(self.json_preifx, name)
                job_name, job_id = "_".join(name[0].split("_")[:-1]), name.split(".")[0].split("_")[-1]
                # Only surviving tasks will we try to resume
                if determine_job_is_alive(job_id):
                    with open(pf, "rb") as f:
                        data = pickle.load(f)
                    with self._lock:
                        self.jobname_map.update({job_name: data[0]})
                        self._job_map.update({job_id: data[1]})
                        job = self._job_map[job_id]
                        for _, rank_info in job.rank_map.items():
                            rank_info.last_report_time = time.time()  # prevent timeout
                    logger.info(f"resume job:{job_name}, jobid:{job_id} from dump file.")
                else:
                    try:
                        os.remove(pf)
                    except Exception:
                        logger.error(f"del file: {pf} failed!")
                    else:
                        logger.info(f"del file: {pf} ok!")

        logger.info("resume ok.")

    def make_json_name(self, job_id, job_name):
        return "/".join([self.json_preifx, f"{job_name}_{job_id}.pt"])

    def dump_json(self, job_id, job_name):
        json_name = self.make_json_name(job_id, job_name)
        data = [self.jobname_map[job_name], self._job_map[job_id]]
        with open(f"{json_name}", "wb+") as file:
            pickle.dump(data, file, protocol=pickle.HIGHEST_PROTOCOL)

    def move_json(self, job_id, job_name):
        json_name = self.make_json_name(job_id, job_name)
        try:
            os.makedirs("./json_finish")
            shutil.copyfile(json_name, "./json_finish/" + os.path.basename(json_name))
        except Exception:
            pass

    def stop(self):
        """
        stop the Coordinator
        """
        logger.info("stop the Coordinator...")
        self.stopped = True

    def del_json(self, job_id, job_name):
        """
        If we give up restarting a task, delete its local json file at the same time
        to prevent the Coordinator from reading old data after restarting
        """

        json_name = self.make_json_name(job_id, job_name)
        try:
            os.remove(json_name)
        except FileNotFoundError:
            pass

    def deal_with_register(self, request_info: Dict):
        # We don't want any deal_XX function to throw an exception,
        # so we use a big try except to wrap the business logic code
        try:
            # If we are resuming, first suspend the register process.
            while self.restarting:
                time.sleep(0.5)

            rank, slurm_id, slurm_jobname = (
                request_info["rank"],
                request_info["slurm_jobid"],
                request_info["slurm_jobname"],
            )
            rankinfo = RankInfo(rank=rank, hostname=request_info["hostname"], device_id=request_info["device_id"])
            logger.info(request_info["script_cfg"])
            with self._lock:
                # If you haven't seen this slurm_id, initialize a series of status information
                if slurm_jobname not in self.jobname_map:
                    jobinfo = JobInfo(
                        world_size=request_info["world_size"],
                        slurm_jobname=slurm_jobname,
                        slurm_jobid=slurm_id,
                        script_config=request_info["script_cfg"]
                    )
                    logger.info(f"Register new Jobname: {jobinfo}")
                    self.jobname_map.update({slurm_jobname: jobinfo})  # update jobname_map

                job = self.jobname_map[slurm_jobname]
                job.rank_map.update({rank: rankinfo})
                job.nodelist.add(rankinfo.hostname)
                if slurm_id != job.slurm_jobid:
                    logger.info(f"Restart {job.slurm_jobname} the {job.restart_count}-th time")
                    job.restart_count += 1
                job.slurm_jobid = slurm_id  # The slurm job id received at the time of register must be the latest
                self._job_map.update({slurm_id: job})  # update _job_map

                # All ranks in a job are ready
                if len(job.rank_map) == job.world_size:
                    # Get the list of hostname, followed by screening points
                    # job.nodelist = get_job_hostname(slurm_id)
                    job.job_state = JobState.RUN
                    self.dump_json(slurm_id, slurm_jobname)
                    msg = f"Job {job.slurm_jobname} all processes are ready and start polling thread status, \
has nodelist: {job.nodelist}"
                    logger.info(msg)

            logger.info(f"register: jobname: {slurm_id}, jobid: {slurm_jobname}, rank:{rank}")
            return True
        except Exception as e:
            logger.error(f"deal_with_register() meet feat error{e}, {request_info}")
            return False

    def deal_keep_alive(self, request_info: Dict):
        try:
            jobid, rank, step, ckpt_every = request_info["jobid"], request_info["rank"],  request_info["step"], request_info["ckpt_every"]
            
            job_info = self._job_map[jobid]
            
            if step % ckpt_every == 0:
                job_info.last_ckpt = step
            
            job_info.rank_map[rank].last_report_time = time.time()
            return True
        except KeyError:
            return False
        except Exception:
            return False

    def deal_exception(self, request_info: Dict):
        try:
            jobid, rank = request_info["jobid"], request_info["rank"]
            with self._lock:
                job_info = self._job_map[jobid]
                job_info.rank_map[rank].exception_list.append(request_info["exception_msg"])
            return True
        except KeyError:
            return False
        except Exception:
            return False

    def deal_human_sacncel(self, request_info: Dict):
        try:
            jobid = request_info["jobid"]
            self._job_map[jobid].is_hunman_scancel = True
            return True
        except KeyError:
            return False
        except Exception:
            return False

    def main_thread(self):
        """
        The main loop of the Coordinator continuously calls polling_check to check whether
        the registered job is abnormal, and if so, it will execute the restart logic.
        """
        while not self.stopped:
            time.sleep(5)  # Prevent errors caused by other apis writing to job_map
            restart_job_ids = self.polling_check()
            # If polling_check returns, there is a task that needs to be restarted
            for jobid in restart_job_ids:
                logger.info(f"restart from job {jobid}")
                job = self._job_map[jobid]
                name = job.slurm_jobname
                env = os.environ
                env.update({"COORDIATOR_IP": self._ip, "COORDIATOR_PORT": self._port})

                try:  # Kill tasks that need to be restarted
                    scancel_slurm_job(jobid, env)
                except Exception as e:
                    logger.warning(e)
                else:
                    logger.info(f"scancel {name}, {jobid}")

                # we sleep 1 min for those reasons:
                # 1. Prevent two consecutive restarts belonging to the same folder.
                # 2. Wait for the automatic drain node script to take effect.
                # 3. Wait for the failure node to restart later.
                time.sleep(60)

                if name not in self.restart_job_info:
                    self.restart_job_info.update({name: RestartInfo()})

                reinfo = self.restart_job_info[name]
                reinfo.job_infos.append(copy.deepcopy(job))
                if time.time() - reinfo.latest_restart_time < 1200:
                    reinfo.restart_count += 1
                else:
                    logger.info(f"Reset job: {name} restart-count")
                    # Reboots from a long time ago, we don't count restart attempts
                    reinfo.restart_count = 0

                if reinfo.restart_count >= 3:
                    msg = f'Job "{job.slurm_jobname}" restarts three times and still fails, abort restart.'
                    logger.info(msg)
                    self.delete_job(job.slurm_jobid)
                else:
                    # do the actual restart
                    try:
                        # update LOAD_CKPT_FOLDER
                        load_ckpt = None
                        script_config = handle_scitofloat(job.script_config)
                        script_config = ast.literal_eval(script_config)
                        if not job.last_ckpt and "LOAD_CKPT_FOLDER" in script_config:
                            load_ckpt = os.path.basename(script_config["LOAD_CKPT_FOLDER"])
                        elif job.last_ckpt:
                            load_ckpt = str(job.last_ckpt)
                        
                        if load_ckpt:
                            script_config["LOAD_CKPT_FOLDER"] = os.path.join(script_config["SAVE_CKPT_FOLDER"], load_ckpt)
                            script_config["ckpt"]["load_ckpt_folder"] = script_config["LOAD_CKPT_FOLDER"]
                        job.script_config = str(script_config)
                        
                        # get jobinfo
                        jobinfo = get_slurm_jobinfo(jobid)
                        
                        exclude_nodes = ""
                        if self.is_nccl_test:
                            exclude_nodes = do_find_slow_node(self.nccl_timeout, jobinfo["nodelist"])
                        
                        re, msg = sbatch_slurm_job(jobinfo,job.script_config, exclude_nodes,env=env)
                        if re is False:
                            logger.info(msg)
                    except Exception as e:
                        logger.error(e)
                    else:
                        logger.info(f"launch {job.slurm_jobname}")

                    msg = f"Restart job:{job.slurm_jobname} for the {reinfo.restart_count}-th time"
                    logger.info(msg)
                    self.reset_job_state(jobid)

            # restart all, allow registion.
            self.restarting = False

    def decide_whether_restart(self, format_trace: str) -> MessageLevel:
        format_trace = format_trace.lower()
        # We will not restart the tasks that are canceled
        if "Process received signal".lower() in format_trace:
            return MessageLevel.FATAL

        # We do not restart tasks that have problems with storage (TODO: sometimes temporary exceptions)
        if "s3 storage service may get problem".lower() in format_trace or "upload file".lower() in format_trace:
            return MessageLevel.FATAL

        # If the loss is flying, it will not be restarted for the time being
        # (need to set a standard with the airline)
        # if "Loss spike may be happened in step".lower() in format_trace:
        #     return MessageLevel.IGNORE

        # ERROR needs to be restarted.
        if "CUDA".lower() in format_trace or "NCCL".lower() in format_trace:
            return MessageLevel.ERROR

        if "Device".lower() in format_trace:
            hostname = re.findall(r"`(.*?)`", format_trace)[1]
            prefix = os.environ["JOB_NAME"]
            if os.path.exists(os.path.join(prefix, "exclude_nodes.log")):
                with open(os.path.join(prefix, "exclude_nodes.log"), "r+") as f:
                    exclude_nodes = f.read().splitlines()
                    if hostname.upper() not in exclude_nodes:
                        f.write(hostname.upper() + "\n")
            else:
                with open(os.path.join(prefix, "exclude_nodes.log"), "a+") as f:
                    f.write(hostname.upper() + "\n")
            return MessageLevel.ERROR

        if "completed".lower() in format_trace:
            return MessageLevel.COMPLETE

        return MessageLevel.IGNORE

    def delete_job(self, jid: int):
        job = self._job_map[jid]
        logger.info(f"Del job: {job.slurm_jobname}")
        self.reset_job_state(jid)
        if job.job_state == JobState.COMPLETE:
            self.move_json(jid, job.slurm_jobname)
        else:
            self.del_json(jid, job.slurm_jobname)

    def cut_error_lens(self, error: str):
        if len(error) <= 20:
            return error
        else:
            return error[-20:]

    def polling_check(self):
        logger.info("Polling thread is launch!")
        while not self.stopped:
            restart_job_ids = set()
            delete_job_ids = set()
            jtable = PrettyTable()
            jtable.field_names = ["Job Name", "Slurm ID", "Worldsize", "Status", "Restart Count"]

            rtable = PrettyTable()
            rtable.field_names = [
                "Job Name",
                "Rank",
                "HostName",
                "device ID",
                "Latest heartbeat(s)",
                "exceptions",
                "Type",
            ]

            # Loop through all jobs
            job_map = copy.deepcopy(self._job_map)
            for jobid, job_info in job_map.items():
                if job_info.is_hunman_scancel:  # Check if we killed it
                    logger.info(f'Human scncael job: "{jobid}" name:"{job_info.slurm_jobname}"')
                    delete_job_ids.add(jobid)
                    continue

                rank_dict = job_info.rank_map

                # traverse all ranks
                def job_state_check(record=False):
                    nonlocal restart_job_ids, delete_job_ids, rank_dict, jobid, job_info, rtable
                    catch_expection = False
                    for rank, rankinfo in rank_dict.items():  # pylint: disable=W0640
                        # Heartbeat timeout detection
                        if time.time() - rankinfo.last_report_time > self._timeout:
                            catch_expection = True
                            msg = f"{rankinfo} keep alive TIMEOUT for {self._timeout} s"
                            logger.info(msg)
                            
                            if not determine_job_is_alive(jobid):
                                delete_job_ids.add(jobid)
                            else:
                                restart_job_ids.add(jobid)  # pylint: disable=W0640

                        # loop the exception list of rank
                        if len(rankinfo.exception_list) > 0:
                            catch_expection = True
                            for e in rankinfo.exception_list:
                                slurm_jobname = job_info.slurm_jobname  # pylint: disable=W0640
                                err: str = e["error"]
                                level = self.decide_whether_restart(err)
                                msg = None
                                if level == MessageLevel.FATAL:
                                    msg = f"Job:{slurm_jobname} Caught a fatal \
error that cannot restart, please restart manually. Error : {e}"
                                    delete_job_ids.add(jobid)  # pylint: disable=W0640
                                    job_info.job_state = JobState.ABORT  # pylint: disable=W0640
                                elif level == MessageLevel.IGNORE:
                                    msg = f"Job:{slurm_jobname} Caught a ignore error, continue. Error: {e}"
                                elif level == MessageLevel.ERROR:
                                    msg = f"Job:{slurm_jobname} Caught a restartable error. Error : {e}"
                                    # ERROR status task we try to restart.
                                    restart_job_ids.add(jobid)  # pylint: disable=W0640
                                    job_info.job_state = JobState.ERROR  # pylint: disable=W0640
                                elif level == MessageLevel.COMPLETE:
                                    delete_job_ids.add(jobid)  # pylint: disable=W0640
                                    job_info.job_state = JobState.COMPLETE  # pylint: disable=W0640

                                if msg:
                                    do_alert = True
                                    if level == MessageLevel.IGNORE:
                                        err_header = self.cut_error_lens(str(e))
                                        if err_header not in self.fired_error:
                                            self.fired_error.add(err_header)
                                        else:
                                            do_alert = False

                                    if do_alert:
                                        logger.info(msg)

                                if record:
                                    # In fact, we can do news deduplication here.
                                    rtable.add_row(
                                        [
                                            slurm_jobname,  # pylint: disable=W0640
                                            rank,
                                            rankinfo.hostname,
                                            rankinfo.device_id,
                                            round(time.time() - float(rankinfo.last_report_time), 2),
                                            err[-40:],
                                            level,
                                        ]
                                    )

                    return catch_expection

                catch_expection = job_state_check()
                if catch_expection is True:
                    time.sleep(self._max_waiting_seconds)  # Wait for all rank exceptions to arrive.
                    job_state_check(record=True)  # check again.

                jtable.add_row(
                    [
                        job_info.slurm_jobname,
                        job_info.slurm_jobid,
                        job_info.world_size,
                        "Error" if catch_expection else "OK",
                        job_info.restart_count,
                    ]
                )
                self.dump_json(jobid, job_info.slurm_jobname)  # save the state of the current task

            print(f"+++++++++++++++++++++++++{now_time()}++++++++++++++++++++")
            print(jtable)
            print(rtable)
            print("")

            intersection_job = restart_job_ids & delete_job_ids  # find the intersection
            restart_job_ids -= intersection_job  # If a task is del, it cannot be restarted

            # There is a fatal error reporting task, we will delete it from '_job_map' and
            # give up the monitoring of the task (but not cancel him?).
            # In this place, we del drop the key of '_job_map/_rank_map', 'deal_keep_alive'
            # and 'deal_exception' may report KeyError,
            # but it should not be a big problem if we catch it.
            for del_job in delete_job_ids:
                self.delete_job(del_job)

            if len(restart_job_ids) > 0:
                self.restarting = True
                break

            time.sleep(self._max_waiting_seconds)

        return restart_job_ids


def create_coordinator_app(coordinator: Coordinator):
    app = Flask(__name__)

    def build_ret(code, info=""):
        return {"code": code, "info": info}

    @app.route("/coordinator/register", methods=["POST"])
    def register():
        coordinator.last_activity = time.time()
        ret_info = coordinator.deal_with_register(request.json)
        if ret_info:
            return build_ret(0, ret_info)
        else:
            return build_ret(1)

    @app.route("/coordinator/keep_alive", methods=["POST"])
    def keep_alive():
        coordinator.last_activity = time.time()
        ret_info = coordinator.deal_keep_alive(request.json)
        logger.info(f"heartbeat: {request.json}")
        if ret_info:
            return build_ret(0)
        else:
            return build_ret(1)

    @app.route("/coordinator/catch_exception", methods=["POST"])
    def catch_exception():
        coordinator.last_activity = time.time()
        ret_info = coordinator.deal_exception(request.json)
        if ret_info:
            return build_ret(0)
        else:
            return build_ret(1)

    @app.route("/coordinator/get_human_scancel", methods=["POST"])
    def handle_human_scancel():
        coordinator.last_activity = time.time()
        ret_info = coordinator.deal_human_sacncel(request.json)
        if ret_info:
            return build_ret(0)
        else:
            return build_ret(1)

    return app




parser = argparse.ArgumentParser()

if __name__ == "__main__":
    parser.add_argument("--port", type=int, default=portpicker.pick_unused_port(), help="coordinator port")
    parser.add_argument("--nccl_test", action="store_true", help="nccl test to find slow nodes")
    parser.add_argument("--nccl_timeout", type=int, default=None, help="timeout for nccl test")
    args = parser.parse_args()
    
    if args.nccl_test and args.nccl_timeout is None:
        raise RuntimeError("nccl_timeout should be set if `nccl_test` is true.")
    
    debug = False
    coordinator_timeout = 240

    if "http_proxy" in os.environ:
        del os.environ["http_proxy"]
    if "https_proxy" in os.environ:
        del os.environ["https_proxy"]
    if "HTTP_PROXY" in os.environ:
        del os.environ["HTTP_PROXY"]
    if "HTTPS_PROXY" in os.environ:
        del os.environ["HTTPS_PROXY"]

    hostname = socket.gethostname()
    ipaddr = socket.gethostbyname(hostname)
    
    with open("coordinator_env", "w", encoding="utf-8") as f:
        f.write(f"COORDIATOR_IP={ipaddr}\n")
        f.write(f"COORDIATOR_PORT={args.port}\n")

    def coordinator_run():
        coordinator = Coordinator(ipaddr, str(args.port), args.nccl_test, args.nccl_timeout)
        coordinator_app = create_coordinator_app(coordinator)
        socketio = SocketIO(coordinator_app)

        def check_activity():
            while True:
                current_time = time.time()
                if current_time - coordinator.last_activity > coordinator_timeout:
                    coordinator.stop()
                    time.sleep(120)  # wait coordinator stop
                    socketio.stop()
                    break
                eventlet.sleep(5)

        if debug:
            serve(
                coordinator_app,
                host=ipaddr,
                port=int(args.port),
                threads=64,
            )
        else:
            eventlet.spawn(check_activity)
            socketio.run(coordinator_app, host=ipaddr, port=int(args.port), debug=False, use_reloader=False)

    coordinator_run()
