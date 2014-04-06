# Based on
# https://github.com/CloudifySource/cloudify-recipes/blob/
# 991ab4ce0596930836f7d4e33f6f9bd70894d85a/
# services/puppet/PuppetBootstrap.groovy
import datetime
import inspect
import json
import os
import requests
import platform
import re
import subprocess
import tempfile
import urlparse


PUPPET_CONF_TPL = """# This file was generated by Cloudify
[main]
    ssldir = /var/lib/puppet/ssl
    environment = {environment}
    pluginsync = true
    logdir = /var/log/puppet
    vardir = /var/lib/puppet
    classfile = $vardir/classes.txt
    factpath = /opt/cloudify/puppet/facts:$vardir/lib/facter:$vardir/facts
    modulepath = {modulepath}

[agent]
    server = {server}
    certname = {certname}
    node_name_value = {node_name}
"""

PUPPET_CONF_MODULE_PATH = [
    '/etc/puppet/modules',
    '/usr/share/puppet/modules',
    '/opt/cloudify/puppet/modules',
    # {cloudify_module_path}
]
# docs.puppetlabs.com/puppet/latest/reference/lang_reserved.html#tags
PUPPET_TAG_RE = re.compile('\A[a-z0-9_][a-z0-9_:\.\-]*\Z')
# docs.puppetlabs.com/puppet/latest/reference/lang_reserved.html#environments
PUPPET_ENV_RE = re.compile('\A[a-z0-9]+\Z')


class PuppetError(RuntimeError):
    """An exception for all Puppet related errors"""


class SudoError(PuppetError):

    """An internal exception for failures when running
    an OS command with sudo"""


class PuppetInternalLogicError(PuppetError):
    pass


class PuppetParamsError(PuppetError):
    """ Invalid parameters were supplied """


def _context_to_struct(ctx):
    ret = {
        'node_id': ctx.node_id,
        'blueprint_id': ctx.blueprint_id,
        'deployment_id': ctx.deployment_id,
        'properties': ctx.properties,
        'runtime_properties': ctx.runtime_properties,
        'capabilities': {},
    }
    if hasattr(ctx, 'capabilities'):
        ret['capabilities'] = ctx.capabilities.get_all()
    return ret


class PuppetManager(object):

    EXTRA_PACKAGES = []
    DEFAULT_VERSION = '3.4.3-1puppetlabs1'
    DIRS = {
        'local_repo': '~/cloudify/puppet',
        'local_custom_facts': '/opt/cloudify/puppet/facts',
        'cloudify_module': '/opt/cloudify/puppet/modules/cloudify',
    }
    metadata_file_path = '/opt/cloudify/puppet/metadata.json'

    # Copy+paste from Chef plugin - start
    def _log_text(self, title, prefix, text):
        ctx = self.ctx
        if not text:
            return
        ctx.logger.info('*** ' + title + ' ***')
        for line in text.splitlines():
            ctx.logger.info(prefix + line)

    def _sudo(self, *args):
        """a helper to run a subprocess with sudo, raises SudoError"""

        ctx = self.ctx

        def get_file_contents(f):
            f.flush()
            f.seek(0)
            return f.read()

        cmd = ["/usr/bin/sudo"] + list(args)
        ctx.logger.info("Running: '%s'", ' '.join(cmd))

        # TODO: Should we put the stdout/stderr in the celery logger?
        #       should we also keep output of successful runs?
        #       per log level? Also see comment under run_chef()
        stdout = tempfile.TemporaryFile('rw+b')
        stderr = tempfile.TemporaryFile('rw+b')
        out = None
        err = None
        try:
            subprocess.check_call(cmd, stdout=stdout, stderr=stderr)
            out = get_file_contents(stdout)
            err = get_file_contents(stderr)
            self._log_text("stdout", "  [out] ", out)
            self._log_text("stderr", "  [err] ", err)
        except subprocess.CalledProcessError as exc:
            raise SudoError("{exc}\nSTDOUT:\n{stdout}\nSTDERR:{stderr}".format(
                exc=exc,
                stdout=get_file_contents(stdout),
                stderr=get_file_contents(stderr)))
        finally:
            stdout.close()
            stderr.close()

        return out, err

    def _sudo_write_file(self, filename, contents):
        """a helper to create a file with sudo"""
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(contents)

        self._sudo("mv", temp_file.name, filename)
    # Copy+paste from Chef plugin - end

    # http://stackoverflow.com/a/5953974
    def __new__(cls, ctx):
        """ Transparent factory. PuppetManager() returns a subclass. """
        if cls is PuppetManager:
            c = cls._get_class()
            ctx.logger.debug("PuppetManager class: {0}".format(c))
            return super(PuppetManager, cls).__new__(c)
        # Disable magic for subclasses
        return super(PuppetManager, cls).__new__(c, ctx)

    @staticmethod
    def _get_class():
        classes = filter(inspect.isclass, globals().values())
        classes = [c for c in classes if issubclass(c, PuppetManager)]
        classes = [c for c in classes if c is not PuppetManager]
        classes = [c for c in classes if c._handles()]
        if len(classes) != 1:
            raise PuppetInternalLogicError(
                "Failed to find correct PuppetManager")
        return classes[0]

    def __init__(self, ctx):
        self.ctx = ctx
        self.props = self.ctx.properties['puppet_config']
        self._process_properties()

    def _process_properties(self):
        p = self.props
        if 'environment' not in p:
            raise PuppetParamsError("puppet_config.environment is missing")
        env = re.sub('[- .]', '_', p['environment'])
        if not PUPPET_ENV_RE.match(env):
            raise PuppetParamsError(
                "puppet_config.environment must contain only alphanumeric "
                "characters or underscores, you gave '{0}'".format(env))
        self.environment = env
        for tag in p.get('tags', []):
            if not PUPPET_TAG_RE.match(tag):
                raise PuppetParamsError(
                    "puppet_config.tags[*] must match {0}, you gave "
                    "'{1}'".format(PUPPET_TAG_RE, tag))
        if 'server' not in p:
            raise PuppetParamsError("puppet_config.server is missing")

    def install(self):
        url = self.get_repo_package_url()
        response = requests.head(url)
        if response.status_code != requests.codes.ok:
            raise PuppetError("Repo package is not available (at {0})".format(
                url))

        self.ctx.logger.info("Installing package from {0}".format(url))
        self.install_package_from_url(url)
        self.refresh_packages_cache()
        self.install_package('puppet',
                             self.props.get('version', self.DEFAULT_VERSION))
        for package_name in self.EXTRA_PACKAGES:
            self.install_package(package_name)

        self.dirs = {k: os.path.expanduser(v) for k, v in self.DIRS.items()}
        self._sudo("mkdir", "-p", *self.dirs.values())
        self._sudo("chmod", "700", *self.dirs.values())
        self.install_custom_facts()
        self.configure()

    def configure(self):
        p = self.props
        node_name = (
            p.get('node_name_prefix', '') +
            self.ctx.node_id +
            p.get('node_name_suffix', '')
        )
        certname = (
            datetime.datetime.utcnow().strftime('%Y%m%d%H%M') +
            '-' +
            node_name
        )
        local_modules_path = os.path.join(self.dirs['local_repo'], 'modules')

        conf = PUPPET_CONF_TPL.format(
            environment=self.environment,
            modulepath=':'.join(
                PUPPET_CONF_MODULE_PATH + [local_modules_path]),
            server=self.props['server'],
            certname=certname,
            node_name=node_name,
        )
        self._sudo_write_file('/etc/puppet/puppet.conf', conf)

    def refresh_packages_cache(self):
        pass

    def install_custom_facts(self):
        facts_source_path = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            'puppet', 'facts', 'cloudify_facts.rb')
        facts_destination_path = self.DIRS['local_custom_facts']
        self.ctx.logger.info("Installing custom facts {0} to {1}".format(
            facts_source_path,
            facts_destination_path))
        self._sudo('cp', facts_source_path, facts_destination_path)

    def run(self):
        facts = self.props.get('facts', {})
        if 'cloudify' in facts:
            raise PuppetError("Puppet attributes must not contain 'cloudify'")
        facts = _context_to_struct(self.ctx)
        if ctx.related:
            facts['cloudify']['related'] = _context_to_struct(ctx.related)
        t = 'puppet.{0}.{1}.{2}.'.format(
            ctx.node_name, ctx.node_id, os.getpid())
        temp_file = tempfile.NamedTemporaryFile
        facts_file = temp_file(prefix=t, suffix=".facts_in.json", delete=False)
        json.dump(facts, facts_file, indent=4)
        facts_file.close()

        cmd = [
            "puppet", "agent",
            "--onetime", "--no-daemonize",
            "--logdest", "console",
            "--logdest", "syslog"
        ]

        tags = self.props.get('tags', [])

        if self.props.get('add_operation_tag', False):
            tags += ['cloudify_operation_' + ctx.operation]

        if tags:
            cmd += ['--tags', ','.join(self.props['tags'])]

        cmd = ' '.join(cmd)

        run_file = temp_file(prefix=t, suffix=".run.sh", delete=False)
        run_file.write(
            '#!/bin/bash -e\n'
            'export FACTERLIB={0}\n'
            'export CLOUDIFY_FACTS_FILE={1}\n'
            .format(self.DIRS['local_custom_facts'], facts_file.name) +
            cmd + '\n'
        )
        run_file.close()
        self._sudo('chmod', '+x', run_file.name)
        self.ctx.logger.info("Will run: '{0}' (in {1})".format(cmd, 
                                                               run_file.name))
        self._sudo(run_file.name)

        os.remove(facts_file.name)


class RubyGemJsonExtraPackageMixin(object):
    EXTRA_PACKAGES = ["rubygem-json"]


class DebianPuppetManager(PuppetManager):

    @staticmethod
    def _handles():
        return platform.linux_distribution()[0].lower() in (
            'debian', 'ubuntu', 'mint')

    def get_repo_package_url(self):
        ver = platform.linux_distribution()
        if ver[2]:
            ver = ver[2]
        else:
            if ver[1].endswith('/sid'):
                ver = 'sid'
            else:
                raise PuppetError("Fail to detect Linux distro version")

        url = self.props.get('repos', {}).get('deb', {}).get(ver)
        return (
            url
            or
            'http://apt.puppetlabs.com/puppetlabs-release-{0}.deb'.format(ver))

    def install_package_from_url(self, url):

        name = os.path.basename(urlparse.urlparse(url).path)

        pkg_file = tempfile.NamedTemporaryFile(suffix='.'+name, delete=False)
        self.ctx.logger.info("Using temp file {0} for package installation".
                             format(pkg_file.name))
        pkg_file.write(requests.get(url).content)
        pkg_file.flush()
        pkg_file.close()
        self._sudo('dpkg', '-i', pkg_file.name)
        os.remove(pkg_file.name)

    def refresh_packages_cache(self):
        return  # XXX: dev mode
        self._sudo('apt-get', 'update')

    # XXX: package_version is not sanitized
    def install_package(self, package_name, package_version=None):
        if package_version is None:
            p = package_name
        else:
            p = package_name + '=' + str(package_version)
        self._sudo('apt-get', 'install', '-y', p)


class RHELPuppetManager(RubyGemJsonExtraPackageMixin, PuppetManager):

    @staticmethod
    def _handles():
        return platform.linux_distribution()[0] in (
            'redhat', 'centos', 'fedora')

#     def get_repo_package_url(self):
#         ver = platform.linux_distribution()[1].partition('/')[0]
#         return 'http://apt.puppetlabs.com/puppetlabs-release-{0}.deb'.format(
#             ver)

    def install_package_from_url(self, url):
        self._sudo("rpm", "-ivh", url)


from cloudify.mocks import MockCloudifyContext
ctx = MockCloudifyContext(
    node_name='node_name',
    node_id=datetime.datetime.utcnow().strftime('node_name_%Y%m%d_%H%M%S'),
    operation='create',
    properties={
    'puppet_config': {
        'add_operation_tag': True,
        'environment': 'e1',
        'tags': ['a', 'b'],
        'server': 'puppet',
        'node_name_prefix': 'pfx-',
        'node_name_suffix': '.puppet.example.com',
    }
})

c = PuppetManager(ctx)
c.install()
c.run()
