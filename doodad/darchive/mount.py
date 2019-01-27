"""
These objects are pointers to code/data you wish to give access
to a launched job.

Each object defines a source and a mount point (where the directory will be visible
to the launched process)

"""
import os
import shutil
import tarfile
import tempfile
from contextlib import contextmanager

from doodad.apis import aws_util
from doodad import utils 


class Mount(object):
    """
    Args:
        mount_point (str): Location of directory visible to the running process *inside* container
        pythonpath (bool): If True, adds this folder to the $PYTHON_PATH environment variable
        output (bool): If False, this is a "code" directory. If True, this should be an empty
            "output" directory (nothing will be copied to remote)
    """
    def __init__(self, mount_point=None, pythonpath=False, output=False):
        self.pythonpath = pythonpath
        self.read_only = not output
        self.mount_point = mount_point
        self._name = None
        self.local_dir = None

    def dar_build_archive(self, deps_dir):
        raise NotImplementedError()

    def dar_extract_command(self):
        raise NotImplementedError()

    @property
    def writeable(self):
        return not self.read_only

    @property
    def name(self):
        return self._name

    def __str__(self):
        return '%s@%s'% (type(self).__name__, self.name)

    @contextmanager
    def gzip(self, filter_ext=(), filter_dir=()):
        """
        Return filepath to a gzipped version of this directory for uploading
        """
        assert self.read_only
        def filter_func(tar_info):
            filt = any([tar_info.name.endswith(ext) for ext in filter_ext]) or \
                   any([ tar_info.name.endswith('/'+ext) for ext in filter_dir])
            if filt:
                return None
            return tar_info
        with tempfile.NamedTemporaryFile('wb+', suffix='.tar') as tf:
            # make a tar.gzip archive of directory
            with tarfile.open(fileobj=tf, mode="w") as tar:
                tar.add(self.local_dir, arcname=os.path.basename(self.local_dir), filter=filter_func)
            tf.seek(0)
            yield tf.name


class MountLocal(Mount):
    """
    """
    def __init__(self, local_dir, mount_point=None, cleanup=True,
                filter_ext=('.pyc', '.log', '.git', '.mp4'),
                filter_dir=('data',),
                **kwargs):
        super(MountLocal, self).__init__(mount_point=mount_point, **kwargs)
        self.local_dir = os.path.realpath(os.path.expanduser(local_dir))
        self._name = self.local_dir.replace('/', '_')
        self.sync_dir = local_dir
        self.cleanup = cleanup
        self.filter_ext = filter_ext
        self.filter_dir = filter_dir
        if mount_point is None:
            self.mount_point = self.local_dir
        else:
            assert not self.mount_point.endswith('/'), "Do not end mount points with backslash"

    def dar_build_archive(self, deps_dir):
        dep_dir = os.path.join(deps_dir, 'local', self.name)
        os.makedirs(os.path.join(deps_dir, 'local'))
        shutil.copytree(self.local_dir, dep_dir)

        extract_file = os.path.join(dep_dir, 'extract.sh')
        with open(extract_file, 'w') as f:
            mount_point = os.path.dirname(self.mount_point)
            f.write('mkdir -p %s\n' % mount_point)
            #print(dep_dir) 
            #f.write('ln -s $USER_PWD/archive/deps/local/%s %s\n' % (self.name, self.mount_point))
            f.write('mv ./deps/local/{name} {mount}'.format(name=self.name, mount=self.mount_point))
        os.chmod(extract_file, 0o777)

    def dar_extract_command(self):
        return './deps/local/{name}/extract.sh'.format(
            name=self.name,
        )

    def __str__(self):
        return 'MountLocal@%s'%self.local_dir

    def docker_mount_dir(self):
         return os.path.join('/mounts', self.mount_point.replace('~/',''))


class MountGit(Mount):
    def __init__(self, git_url, branch=None,
                 ssh_identity=None, **kwargs):
        super(MountGit, self).__init__(output=False, **kwargs)
        self.git_url = git_url
        self.repo_name = os.path.splitext(os.path.split(git_url)[1])[0]
        assert self.mount_point.endswith(self.repo_name)
        self.ssh_identity = ssh_identity
        self.branch = branch
        self._name = self.repo_name

    def dar_build_archive(self, deps_dir):
        dep_dir = os.path.join(deps_dir, 'git', self.name)
        os.makedirs(dep_dir)
        
        extract_file = os.path.join(dep_dir, 'extract.sh')
        with open(extract_file, 'w') as f:
            mount_point = os.path.dirname(self.mount_point)
            f.write('mkdir -p %s\n' % mount_point)
            f.write('pushd %s > /dev/null\n' % mount_point)
            if self.ssh_identity:
                f.write("GIT_SSH_COMMAND='ssh -i {id}' git clone --quiet {repo_url}\n".format(id=self.ssh_identity, repo_url=self.git_url))
            else:
                f.write("git clone --quiet {repo_url}\n".format(repo_url=self.git_url))
            if self.branch:
                f.write('cd {repo_name}\n'.format(repo_name=self.repo_name))
                f.write('git checkout --quiet {branch}\n'.format(branch=self.branch))
            f.write('popd > /dev/null')
        os.chmod(extract_file, 0o777)

    def dar_extract_command(self):
        # execute the script to pull from S3
        return './deps/git/{name}/extract.sh'.format(
            name=self.name,
        )


class MountS3(Mount):
    def __init__(self, 
                region,
                s3_bucket,
                s3_path, 
                local_dir=None,
                sync_interval=15, 
                output=False,
                dry=False,
                include_types=('*.txt', '*.csv', '*.json', '*.gz', '*.tar', '*.log', '*.pkl'), 
                **kwargs):
        super(MountS3, self).__init__(output=output, **kwargs)
        # load from config
        self.s3_bucket = s3_bucket
        self.s3_path = s3_path
        self.region = region
        self.output = output
        self.sync_interval = sync_interval
        self.sync_on_terminate = True
        self.dry = dry
        self.include_types = include_types
        self._name = '%s.%s' % (self.s3_bucket, self.s3_path.replace('/', '.'))
        if output is False:
            assert local_dir is not None
            self.local_dir = os.path.realpath(os.path.expanduser(local_dir))

    def __str__(self):
        return 'MountS3@s3://%s/%s'% (self.s3_bucket, self.s3_path)

    @property
    def include_string(self):
        return ' '.join(['--include \'%s\''%type_ for type_ in self.include_types])

    def s3_upload(self, filename, remote_filename=None, dry=False, check_exist=True):
        if remote_filename is None:
            remote_filename = os.path.basename(filename)
        remote_path = 'doodad/mount/'+remote_filename
        if check_exist and not dry:
            if aws_util.s3_exists(self.s3_bucket, remote_path, region=self.region):
                print('\t%s exists! ' % os.path.join(self.s3_bucket, remote_path))
                return 's3://'+os.path.join(self.s3_bucket, remote_path)
        return aws_util.s3_upload(filename, self.s3_bucket, remote_path, dry=dry,
                         region=self.region)

    def dar_build_archive(self, deps_dir):
        dep_dir = os.path.join(deps_dir, 's3', self.name)
        #os.makedirs(os.path.join(deps_dir, 's3'))
        os.makedirs(dep_dir)
        extract_file = os.path.join(dep_dir, 'extract.sh')

        if self.output:
            with open(extract_file, 'w') as f:
                # f.write("mkdir -p {local_code_path}\n".format(local_code_path=mount_point))
                f.write("mkdir -p {remote_dir}\n".format(
                    remote_dir=self.mount_point)
                )
                # Sync interval
                f.write("""
                while /bin/true; do
                    aws s3 sync --exclude '*' {include_string} {log_dir} {s3_path}
                    sleep {periodic_sync_interval}
                done & echo sync initiated
                """.format(
                    include_string=self.include_string,
                    log_dir=self.mount_point,
                    s3_path=self.s3_path,
                    periodic_sync_interval=self.sync_interval
                ))

                # Sync on terminate. This catches the case where the spot
                # instance gets terminated before the user script ends.
                #
                # This is hoping that there's at least 3 seconds between when
                # the spot instance gets marked for  termination and when it
                # actually terminates.
                f.write(r"""
                    while /bin/true; do
                        if [ -z $(curl -Is http://169.254.169.254/latest/meta-data/spot/termination-time | head -1 | grep 404 | cut -d \  -f 2) ]
                        then
                            logger "Running shutdown hook."
                            aws s3 cp --recursive {log_dir} {s3_path}
                            break
                        else
                            # Spot instance not yet marked for termination.
                            # This is hoping that there's at least 3 seconds
                            # between when the spot instance gets marked for
                            # termination and when it actually terminates.
                            sleep 3
                        fi
                    done & echo log sync initiated
                """.format(
                    log_dir=self.mount_point,
                    s3_path=self.s3_path,
                ))
        else:
            # first upload directory to S3
            with self.gzip() as gzip_file:
                gzip_path = os.path.realpath(gzip_file)
                file_hash = utils.hash_file(gzip_path)
                s3_path = self.s3_upload(gzip_path, remote_filename=file_hash+'.tar', dry=self.dry)
            self.path_on_remote = s3_path
            self.local_file_hash = gzip_path

            with open(extract_file, 'w') as f:
                remote_tar_name = '/tmp/'+file_hash+'.tar'
                #mount_point =  os.path.join('/mounts', self.mount_point.replace('~/',''))
                mount_point = self.mount_point
                f.write("aws s3 cp {s3_path} {remote_tar_name}\n".format(s3_path=s3_path, remote_tar_name=remote_tar_name))
                f.write("mkdir -p {local_code_path}\n".format(local_code_path=mount_point))
                f.write("tar -xvf {remote_tar_name} -C {local_code_path}\n".format(
                    local_code_path=mount_point,
                    remote_tar_name=remote_tar_name))
        os.chmod(extract_file, 0o777)

    def dar_extract_command(self):
        # execute the script to pull from S3
        return './deps/s3/{name}/extract.sh'.format(
            name=self.name,
        )


class MountGCP(Mount):
    def __init__(self, 
                gcp_path=None,
                sync_interval=15, 
                output=True,
                dry=False,
                exclude_regex='*.tmp',
                **kwargs):
        """

        Args:
            zone (str): Zone name. i.e. 'us-west1-a'
            gcp_bucket (str): Bucket name
            gcp_path (str): Path underneath bucket. The full path will become
                gs://{gcp_bucket}/{gcp_path}
        """
        super(MountGCP, self).__init__(output=output, **kwargs)
        # load from config
        if gcp_path.startswith('/'):
            raise NotImplementedError('Local dir cannot be absolute')
        else:
            # We store everything into a fixed dir /doodad on the remote machine
            # so GCPMode knows to simply sync /doodad
            # (this is b/c we no longer pass in mounts to the launch mode)
            self.sync_dir = os.path.join('/doodad', gcp_path)
        self.output = output
        self.sync_interval = sync_interval
        self.sync_on_terminate = True
        self.exclude_string = '"'+exclude_regex+'"'
        self._name = self.sync_dir.replace('/', '_')
        self.dry = dry
        assert output

    def dar_build_archive(self, deps_dir):
        return 

    def dar_extract_command(self):
        return 'echo helloMountGCP'