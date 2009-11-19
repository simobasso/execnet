"""
execnet: Elastic Python Deployment.
package for connecting to local and remote Python Interpreters.

(c) 2009, Holger Krekel and others
"""
__version__ = "1.0.0b4"

import execnet.apipkg

execnet.apipkg.initpkg(__name__, {
    'PopenGateway':     '.multi:PopenGateway',
    'SocketGateway':    '.multi:SocketGateway',
    'SshGateway':       '.multi:SshGateway',
    'makegateway':      '.multi:makegateway',
    'HostNotFound':     '.gateway:HostNotFound',
    'XSpec':            '.xspec:XSpec',
    'Group':            '.multi:Group',
    'MultiChannel':     '.multi:MultiChannel',
    'RSync':            '.rsync:RSync',
})
