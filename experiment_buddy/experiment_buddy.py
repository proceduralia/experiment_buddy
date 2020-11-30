import concurrent.futures
import datetime
import fabric
import git
import matplotlib.pyplot as plt
import os
import subprocess
import sys
import tensorboardX
import time
import types
import wandb
import wandb.cli
import yaml

wandb_escape = "^"
hyperparams = None
tb = None
SCRIPTS_PATH = os.path.join(os.path.dirname(__file__), "../slurm_scripts/")
PROFILE = False


def timeit(method):
    if not PROFILE:
        return method

    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()
        print(f'{method.__name__!r}  {(te - ts):2.2f} s')
        return result

    return timed


def register(config_params):
    global hyperparams
    # overwrite CLI parameters
    # fails on nested config object
    for k in config_params.keys():
        if k.startswith(wandb_escape):
            raise NameError(f"{wandb_escape} is a reserved prefix")

    for arg in sys.argv[1:]:
        assert arg[:2] == "--"
        k, v = arg[2:].split("=")
        k = k.lstrip(wandb_escape)
        v = _cast_param(v)

        if k not in config_params.keys():
            raise ValueError(f"Trying to set {k}, but that's not one of {list(config_params.keys())}")
        config_params[k] = v
    # TODO: should only register valid_hyperparams()
    hyperparams = config_params.copy()


def _cast_param(v):
    if "." in v:
        v = float(v)
    else:
        try:
            v = int(v)
        except ValueError:
            pass
    return v


def _valid_hyperparam(key, value):
    if key.startswith("__") and key.endswith("__"):
        return False
    if key == "_":
        return False
    if isinstance(value, (types.FunctionType, types.MethodType, types.ModuleType)):
        return False
    return True


class WandbWrapper:
    _global_step = 0

    @property
    def global_step(self):
        return self._global_step

    @global_step.setter
    def global_step(self, value):
        self._global_step = value

    def __init__(self, experiment_id, project_name):
        # proj name is git root folder name
        print(f"wandb.init(project={project_name}, name={experiment_id})")
        wandb.init(project=project_name, name=experiment_id)

        def register_param(name, value, prefix=""):
            if not _valid_hyperparam(name, value):
                return

            if name == "_extra_modules_":
                for module in value:
                    for __k in dir(module):
                        __v = getattr(module, __k)
                        register_param(__k, __v, prefix=module.__name__.replace(".", "_"))
            else:
                name = prefix + wandb_escape + name
                # if the parameter was not set by a sweep
                if not name in wandb.config._items:
                    print(f"setting {name}={str(value)}")
                    setattr(wandb.config, name, str(value))
                else:
                    print(f"not setting {name} to {str(value)}, str because its already {getattr(wandb.config, name)}, {type(getattr(wandb.config, name))}")

        for k, v in hyperparams.items():
            register_param(k, v)

    def add_scalar(self, tag, scalar_value, global_step=None):
        wandb.log({tag: scalar_value}, step=global_step, commit=False)

    def add_figure(self, tag, figure, global_step=None, close=True):
        wandb.log({tag: figure}, step=global_step, commit=False)
        if close:
            plt.close(figure)

    def add_histogram(self, tag, values, global_step=None):
        wandb.log({tag: wandb.Histogram(values)}, step=global_step, commit=False)


@timeit
def deploy(cluster, sweep_yaml, extra_slurm_headers=None, proc_num=1):
    debug = '_pydev_bundle.pydev_log' in sys.modules.keys()  # or __debug__
    debug = False  # TODO: removeme
    ran_by_slurm = "SLURM_JOB_ID" in os.environ.keys()

    local_run = not cluster

    try:
        git_repo = git.Repo(os.path.dirname(hyperparams["__file__"]))
    except git.InvalidGitRepositoryError:
        raise ValueError("the main file be in the repository root")

    project_name = git_repo.remotes.origin.url.split('.git')[0].split('/')[-1]

    if local_run and sweep_yaml:
        raise NotImplemented("Local sweeps are not supported")

    if ran_by_slurm:
        print("using wandb")
        experiment_id = f"{git_repo.head.commit.message.strip()}"
        jid = os.environ["SLURM_JOB_ID"]
        return WandbWrapper(f"{experiment_id}_{jid}", project_name=project_name)

    dtm = datetime.datetime.now().strftime("%b%d_%H-%M-%S") + ".pt/"
    if debug:
        logdir = os.path.join(git_repo.working_dir, "tensorboard/DEBUG_RUN", dtm)
        return _setup_tb(logdir=logdir)

    experiment_id = _ask_experiment_id(cluster, sweep_yaml)
    if local_run:
        logdir = os.path.join(git_repo.working_dir, "tensorboard/", experiment_id, dtm)
        return _setup_tb(logdir=logdir)
    else:
        _commit_and_sendjob(experiment_id, sweep_yaml, git_repo, project_name, proc_num, extra_slurm_headers)
        sys.exit()


def _ask_experiment_id(cluster, sweep):
    import tkinter.simpledialog

    root = tkinter.Tk()
    title = f'{"[CLUSTER" if cluster else "[LOCAL"}'
    if sweep:
        title = f"{title}-SWEEP"
    title = f"{title}]"

    experiment_id = tkinter.simpledialog.askstring(title, "experiment_id")
    experiment_id = (experiment_id or "no_id").replace(" ", "_")
    root.destroy()

    if cluster:
        experiment_id = f"[CLUSTER] {experiment_id}"
    return experiment_id


def _setup_tb(logdir):
    print("http://localhost:6006")
    return tensorboardX.SummaryWriter(logdir=logdir)


def _ensure_scripts(extra_headers):
    ssh_session = fabric.Connection("mila")
    retr = ssh_session.run("mktemp -d -t experiment_buddy-XXXXXXXXXX")
    tmp_folder = retr.stdout.strip()
    for file_path in os.listdir(SCRIPTS_PATH):
        script_path = SCRIPTS_PATH + file_path
        if extra_headers and file_path in ("localenv_sweep.sh", "srun_python.sh"):
            with open(SCRIPTS_PATH + file_path) as fin:
                rows = fin.readlines()

            script_path = "/tmp/" + file_path
            with open(script_path, "w") as fout:
                for flag_idx in range(1, len(rows)):
                    old = rows[flag_idx - 1].strip()
                    new = rows[flag_idx].strip()
                    if old[:7] in ("#SBATCH", "") and new[:7] not in ("#SBATCH", ""):
                        rows.insert(flag_idx, "\n" + extra_headers + "\n")
                        break
                fout.write("".join(rows))

        ssh_session.put(script_path, tmp_folder + "/")
    return tmp_folder, ssh_session


def log_cmd(cmd, retr):
    print("################################################################")
    print(f"## {cmd}")
    print("################################################################")
    print(retr)
    print("################################################################")


@timeit
def _commit_and_sendjob(experiment_id, sweep_yaml: str, git_repo, project_name, proc_num, extra_slurm_header):
    git_url = git_repo.remotes[0].url
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        scripts_folder = executor.submit(_ensure_scripts, extra_slurm_header)
        code_version = git_sync(experiment_id, git_repo)

        _, entrypoint = os.path.split(sys.argv[0])
        if sweep_yaml:
            with open(sweep_yaml, 'r') as stream:
                data_loaded = yaml.safe_load(stream)

            if data_loaded["program"] != entrypoint:
                raise ValueError(f'YAML {data_loaded["program"]} does not match the entrypoint {entrypoint}')
            wandb_stdout = subprocess.check_output(["wandb", "sweep", "--name", experiment_id, "-p", project_name, sweep_yaml], stderr=subprocess.STDOUT).decode("utf-8")
            # sweep_id = wandb_stdout.split("/")[-1].strip()
            row, = [row for row in wandb_stdout.split("\n") if "Run sweep agent with:" in row]
            sweep_id = row.split()[-1].strip()

            ssh_args = (git_url, sweep_id, code_version)
            ssh_command = "/opt/slurm/bin/sbatch {0}/localenv_sweep.sh {1} {2} {3}"
            num_repeats = 1  # this should become > 1 for parallel sweeps
        else:
            _, entrypoint = os.path.split(sys.argv[0])
            ssh_args = (git_url, entrypoint, code_version)
            ssh_command = "bash -l {0}/run_experiment.sh {1} {2} {3}"
            num_repeats = 1  # this should become > 1 for parallel sweeps

    # TODO: assert -e git+git@github.com:manuel-delverme/experiment_buddy.git#egg=experiment_buddy is in requirements.txt
    scripts_folder, ssh_session = timeit(lambda: scripts_folder.result())()
    ssh_command = ssh_command.format(scripts_folder, *ssh_args)
    print(ssh_command)
    for proc_num in range(num_repeats):
        if proc_num > 0:
            time.sleep(1)
            raise NotImplemented
        if proc_num > 1:
            priority = "long"
            raise NotImplemented("localenv_sweep.sh does not handle this yet")

        ssh_session.run(ssh_command)


@timeit
def git_sync(experiment_id, git_repo):
    # 2) commits everything to git with the name as message (so i r later reproduce the same experiment)
    os.system(f"git add .")
    os.system(f"git commit -m '{experiment_id}'")
    # TODO: ideally the commits should go to a parallel branch so the one in use is not filled with versioning checkpoints
    # 3) pushes the changes to git
    os.system("git push")  # TODO: only if commit
    code_version = git_repo.commit().hexsha
    return code_version