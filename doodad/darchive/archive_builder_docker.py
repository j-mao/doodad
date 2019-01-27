"""
Library for building runnable Doodad Archives.

Doodad Archives package code and data into a single
executable shell script, which runs within a docker container.

Currently, doodad uses makeself as a backend to build these
packaged scripts.
"""
import os
import sys
import tempfile
import shutil
import time
import subprocess
import uuid
import contextlib
import uuid

import doodad
from doodad.darchive import mount
from doodad.utils import cmd_builder

THIS_FILE_DIR = os.path.dirname(__file__)
MAKESELF_PATH = os.path.join(THIS_FILE_DIR, 'makeself.sh')
MAKESELF_HEADER_PATH = os.path.join(THIS_FILE_DIR, 'makeself-header.sh')
BEGIN_HEADER = '--- BEGIN DAR OUTPUT ---'

def build_archive(archive_filename='runfile.dar', 
                  docker_image='ubuntu:18.04',
                  payload_script='',
                  mounts=(),
                  verbose=False):
    """
    Construct a Doodad Archive

    Args:
        archive_filename (str): Name of file to save constructed archive script
        docker_image (str): Name of docker image
        payload_script (str): A command or sequence of shell commands to be 
            executed inside the container on when the script is run.
        mounts (tuple): A list of Mount objects
    
    Returns:
        str: Name of archive file.
    """
    # create a temporary work directory
    try:
        work_dir = tempfile.mkdtemp()
        archive_dir = os.path.join(work_dir, 'archive')
        os.makedirs(archive_dir)

        deps_dir = os.path.join(archive_dir, 'deps')
        os.makedirs(deps_dir)
        for mnt in mounts:
            mnt.dar_build_archive(deps_dir)
        
        write_run_script(archive_dir, mounts, 
            payload_script=payload_script, verbose=verbose) 
        write_docker_hook(archive_dir, docker_image, mounts, verbose=verbose)
        write_metadata(archive_dir)

        # create the self-extracting archive
        compile_archive(archive_dir, archive_filename, verbose=verbose)
    finally:
        shutil.rmtree(work_dir)
    return archive_filename

def write_metadata(arch_dir):
    with open(os.path.join(arch_dir, 'METADATA'), 'w') as f:
        f.write('doodad_version=%s\n' % doodad.__version__)
        f.write('unix_timestamp=%d\n' % time.time())
        f.write('uuid=%s\n' % uuid.uuid4())

def write_docker_hook(arch_dir, image_name, mounts, verbose=False):
    docker_hook_file = os.path.join(arch_dir, 'docker.sh')
    builder = cmd_builder.CommandBuilder()
    builder.append('#!/bin/bash')
    mnt_cmd = ''.join([' -v %s:%s' % (mnt.sync_dir, mnt.mount_point) 
        for mnt in mounts if mnt.writeable])
    # mount the script into the docker image
    mnt_cmd += ' -v $(pwd):/payload'
    builder.append('docker run -i {mount_cmds} --user $UID {img} /bin/bash -c "cd /payload;./run.sh"'.format(
        img=image_name,
        mount_cmds=mnt_cmd,
    ))
    with open(docker_hook_file, 'w') as f:
        f.write(builder.dump_script())
    os.chmod(docker_hook_file, 0o777)

def write_run_script(arch_dir, mounts, payload_script, verbose=False):
    runfile = os.path.join(arch_dir, 'run.sh')
    builder = cmd_builder.CommandBuilder()
    builder.append('#!/bin/bash')
    if verbose:
        builder.echo('Running Doodad Archive [DAR] $1')
        builder.echo('DAR build information:')
        builder.append('cat', './METADATA')

    for mount in mounts:
        if verbose:
            builder.append('echo', 'Mounting %s' % mount)
        builder.append(mount.dar_extract_command())
        if mount.pythonpath:
            builder.append('export PYTHONPATH=$PYTHONPATH:%s' % mount.mount_point)
    if verbose:
        builder.append('echo', BEGIN_HEADER)
    builder.append(payload_script)

    with open(runfile, 'w') as f:
        f.write(builder.dump_script())

    os.chmod(runfile, 0o777)

def compile_archive(archive_dir, output_file, verbose=False):
    compile_cmd = "{mkspath} --nocrc --nomd5 --header {mkhpath} {archive_dir} {output_file} {name} {run_script}"
    compile_cmd = compile_cmd.format(
        mkspath=MAKESELF_PATH,
        mkhpath=MAKESELF_HEADER_PATH,
        name='DAR',
        archive_dir=archive_dir,
        output_file=output_file,
        run_script='./docker.sh'
    )
    pipe = subprocess.PIPE
    p = subprocess.Popen(compile_cmd, shell=True, stdout=pipe, stderr=pipe)
    p.wait()
    p.communicate()
    os.chmod(output_file, 0o777)

def run_archive(filename, encoding='utf-8', shell_interpreter='sh', timeout=None):
    if '/' not in filename:
        filename = './'+filename
    p = subprocess.Popen([shell_interpreter, filename, '--quiet'], stdout=subprocess.PIPE)
    output, errcode = p.communicate()
    output = _strip_stdout(output.decode(encoding))
    # strip out 
    return output, errcode


def _strip_stdout(output):
    begin_output = output.find(BEGIN_HEADER, 0) 
    if begin_output >= 0:
        begin_output += len(BEGIN_HEADER)
    output = output[begin_output+1:]
    return output

@contextlib.contextmanager
def temp_archive_file():
    work_dir = tempfile.mkdtemp()
    try:
        archive_file = os.path.join(work_dir, str(uuid.uuid4()).replace('-', '_')+'.dar')
        yield archive_file
    finally:
        shutil.rmtree(work_dir)
