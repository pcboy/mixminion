# Copyright 2002 Nick Mathewson.  See LICENSE for licensing information.
# $Id: Config.py,v 1.31 2003/01/05 06:49:25 nickm Exp $

"""Configuration file parsers for Mixminion client and server
   configuration.

   A configuration file consists of one or more Sections.  Each Section
   has a header and optionally a list of Entries.  Each Entry has a key
   and a value.

   A section header is written as an open bracket, an identifier, and a
   close bracket.  An entry is written as a key, followed optionally by
   a colon or an equal sign, followed by a value.  Values may be split
   across multiple lines as in RFC822.

   Empty lines are permitted between entries, and between entries and
   headers.  Comments are permitted on lines beginning with a '#'.

   All identifiers are case-sensitive.

   Because of cross-platform stupidity, we recognize any sequence of [\r\n]
   as a newline, and who's to tell us we can't?

   Example:

   [Section1]

   Key1 value1
   Key2: Value2 value2 value2
        value2 value2
   Key3 = value3
   # A comment
   Key4=value4
   [Section2]
   Key5 value5
      value5 value5 value5

   We also specify a 'restricted' format in which blank lines,
   comments,  line continuations, and entry formats other than 'key: value'
   are forbidden.  Example:

   [Section1]
   Key1: Value1
   Key2: Value2
   Key3: Value3
   [Section2]
   Key4: Value4

   The restricted format is used for server descriptors.
   """

__all__ = [ 'ConfigError', 'ClientConfig' ]

import calendar
import binascii
import os
import re
import socket # for inet_aton and error
from cStringIO import StringIO

import mixminion.Common
import mixminion.Crypto
from mixminion.Common import MixError, LOG, isPrintingAscii, stripSpace

class ConfigError(MixError):
    """Thrown when an error is found in a configuration file."""
    pass

#----------------------------------------------------------------------
# Validation functions.  These are used to convert values as they appear
# in configuration files and server descriptors into corresponding Python
# objects, and validate their formats

def _parseBoolean(boolean):
    """Entry validation function.  Converts a config value to a boolean.
       Raises ConfigError on failure."""
    s = boolean.strip().lower()
    if s in ("1", "yes", "y", "true", "on"):
        return 1
    elif s not in ("0", "no", "n", "false", "off"):
        raise ConfigError("Invalid boolean %r" % (boolean))
    else:
        return 0

def _parseSeverity(severity):
    """Validation function.  Converts a config value to a log severity.
       Raises ConfigError on failure."""
    s = severity.strip().upper()
    if not mixminion.Common._SEVERITIES.has_key(s):
        raise ConfigError("Invalid log level %r" % (severity))
    return s

def _parseServerMode(mode):
    """Validation function.  Converts a config value to a server mode
       (one of 'relay' or 'local'). Raises ConfigError on failure."""
    s = mode.strip().lower()
    if s not in ('relay', 'local'):
        raise ConfigError("Server mode must be 'Relay' or 'Local'")
    return s

# re to match strings of the form '9 seconds', '1 month', etc.
_interval_re = re.compile(r'''(\d+\.?\d*|\.\d+)\s+
                     (sec|second|min|minute|hour|day|week|mon|month|year)s?''',
                          re.X)
_seconds_per_unit = {
    'second': 1,
    'sec':    1,
    'minute': 60,
    'min':    60,
    'hour':   60*60,
    'day':    60*60*24,
    'week':   60*60*24*7,
    'mon':    60*60*24*30,
    'month':  60*60*24*30,    # These last two aren't quite right, but we
    'year':   60*60*24*365,   # don't need exactness.
    }
_canonical_unit_names = { 'sec' : 'second', 'min': 'minute', 'mon' : 'month' }
def _parseInterval(interval):
    """Validation function.  Converts a config value to an interval of time,
       in the format (number of units, name of unit, total number of seconds).
       Raises ConfigError on failure."""
    inter = interval.strip().lower()
    m = _interval_re.match(inter)
    if not m:
        raise ConfigError("Unrecognized interval %r" % inter)
    num, unit = float(m.group(1)), m.group(2)
    nsec = num * _seconds_per_unit[unit]
    return num, _canonical_unit_names.get(unit,unit), nsec

def _parseInt(integer):
    """Validation function.  Converts a config value to an int.
       Raises ConfigError on failure."""
    i = integer.strip()
    try:
        return int(i)
    except ValueError:
        raise ConfigError("Expected an integer but got %r" % (integer))

# Regular expression to match a dotted quad.
_ip_re = re.compile(r'\d+\.\d+\.\d+\.\d+')

def _parseIP(ip):
    """Validation function.  Converts a config value to an IP address.
       Raises ConfigError on failure."""
    i = ip.strip()

    # inet_aton is a bit more permissive about spaces and incomplete
    # IP's than we want to be.  Thus we use a regex to catch the cases
    # it doesn't.
    if not _ip_re.match(i):
        raise ConfigError("Invalid IP %r" % i)
    try:
        socket.inet_aton(i)
    except socket.error:
        raise ConfigError("Invalid IP %r" % i)

    return i

# Regular expression to match 'address sets' as used in Allow/Deny
# configuration lines. General format is "<IP|*> ['/'MASK] [PORT['-'PORT]]"
_address_set_re = re.compile(r'''(\d+\.\d+\.\d+\.\d+|\*)
                                 \s*
                                 (?:/\s*(\d+\.\d+\.\d+\.\d+))?\s*
                                 (?:(\d+)\s*
                                           (?:-\s*(\d+))?
                                        )?''',re.X)
def _parseAddressSet_allow(s, allowMode=1):
    """Validation function.  Converts an address set string of the form
       'IP/mask port-port' into a tuple of (IP, Mask, Portmin, Portmax).
       Raises ConfigError on failure."""
    s = s.strip()
    m = _address_set_re.match(s)
    if not m:
        raise ConfigError("Misformatted address rule %r", s)
    ip, mask, port, porthi = m.groups()
    if ip == '*':
        if mask != None:
            raise ConfigError("Misformatted address rule %r", s)
        ip,mask = '0.0.0.0','0.0.0.0'
    else:
        ip = _parseIP(ip)
    if mask:
        mask = _parseIP(mask)
    else:
        mask = "255.255.255.255"
    if port:
        port = _parseInt(port)
        if porthi:
            porthi = _parseInt(porthi)
        else:
            porthi = port
        if not 1 <= port <= porthi <= 65535:
            raise ConfigError("Invalid port range %s-%s" %(port,porthi))
    elif allowMode:
        port = porthi = 48099
    else:
        port, porthi = 0, 65535

    return (ip, mask, port, porthi)

def _parseAddressSet_deny(s):
    return _parseAddressSet_allow(s,0)

def _parseCommand(command):
    """Validation function.  Converts a config value to a shell command of
       the form (fname, optionslist). Raises ConfigError on failure."""
    c = command.strip().split()
    if not c:
        raise ConfigError("Invalid command %r" %command)
    cmd, opts = c[0], c[1:]
    if os.path.isabs(cmd):
        if not os.path.exists(cmd):
            raise ConfigError("Executable file not found: %s" % cmd)
        else:
            return cmd, opts
    else:
        # Path is relative
        for p in os.environ.get('PATH', os.defpath).split(os.pathsep):
            p = os.path.expanduser(p)
            c = os.path.join(p, cmd)
            if os.path.exists(c):
                return c, opts

        raise ConfigError("No match found for command %r" %cmd)

def _parseBase64(s,_hexmode=0):
    """Validation function.  Converts a base-64 encoded config value into
       its original. Raises ConfigError on failure."""
    try:
        if _hexmode:
            s = stripSpace(s)
            return binascii.a2b_hex(s)
        else:
            return binascii.a2b_base64(s)
    except (TypeError, binascii.Error, binascii.Incomplete):
        raise ConfigError("Invalid Base64 data")

def _parseHex(s):
    """Validation function.  Converts a hex-64 encoded config value into
       its original. Raises ConfigError on failure."""
    return _parseBase64(s,1)

def _parsePublicKey(s):
    """Validate function.  Converts a Base-64 encoding of an ASN.1
       represented RSA public key with modulus 65537 into an RSA
       object."""
    asn1 = _parseBase64(s)
    if len(asn1) > 550:
        raise ConfigError("Overlong public key")
    try:
        key = mixminion.Crypto.pk_decode_public_key(asn1)
    except mixminion.Crypto.CryptoError:
        raise ConfigError("Invalid public key")
    if key.get_exponent() != 65537:
        raise ConfigError("Invalid exponent on public key")
    return key

# Regular expression to match YYYY/MM/DD
_date_re = re.compile(r"(\d\d\d\d)/(\d\d)/(\d\d)")
def _parseDate(s):
    """Validation function.  Converts from YYYY/MM/DD format to a (long)
       time value for midnight on that date."""
    m = _date_re.match(s.strip())
    try:
        yyyy, MM, dd = map(int, m.groups())
    except (ValueError,AttributeError):
        raise ConfigError("Invalid date %r"%s)
    if not ((1 <= dd <= 31) and (1 <= MM <= 12) and
            (1970 <= yyyy)):
        raise ConfigError("Invalid date %s"%s)
    return calendar.timegm((yyyy,MM,dd,0,0,0,0,0,0))

# Regular expression to match YYYY/MM/DD HH:MM:SS
_time_re = re.compile(r"(\d\d\d\d)/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)")
def _parseTime(s):
    """Validation function.  Converts from YYYY/MM/DD HH:MM:SS format
       to a (float) time value for GMT."""
    m = _time_re.match(s.strip())
    if not m:
        raise ConfigError("Invalid time %r" % s)

    yyyy, MM, dd, hh, mm, ss = map(int, m.groups())
    if not ((1 <= dd <= 31) and (1 <= MM <= 12) and
            (1970 <= yyyy)  and (0 <= hh < 24) and
            (0 <= mm < 60)  and (0 <= ss <= 61)):
        raise ConfigError("Invalid time %r" % s)

    return calendar.timegm((yyyy,MM,dd,hh,mm,ss,0,0,0))

_NICKNAME_CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"+
                   "abcdefghijklmnopqrstuvwxyz"+
                   "0123456789_.!@#-")
MAX_NICKNAME = 128
def _parseNickname(s):
    """Validation function.  Returns true iff s contains a valoid
       server nickname-- that is, a string of 1..128 characters,
       containing only the characters [A-Za-z0-9_.!@#] and '-'.
       """
    s = s.strip()
    bad = s.translate(mixminion.Common._ALLCHARS, _NICKNAME_CHARS)
    if len(bad):
        raise ConfigError("Invalid characters %r in nickname %r" % (bad,s))
    if len(s) > MAX_NICKNAME:
        raise ConfigError("Nickname is too long")
    elif len(s) == 0:
        raise ConfigError("Nickname is too short")
    return s

#----------------------------------------------------------------------

# Regular expression to match a section header.
_section_re = re.compile(r'\[\s*([^\s\]]+)\s*\]')
# Regular expression to match the first line of an entry
_entry_re = re.compile(r'([^:= \t]+)(?:\s*[:=]|[ \t])\s*(.*)')
# Regular expression to match bogus line endings.
_abnormal_line_ending_re = re.compile(r'\r\n?')

def _readConfigFile(contents):
    """Helper function. Given the string contents of a configuration
       file, returns a list of (SECTION-NAME, SECTION) tuples, where
       each SECTION is a list of (KEY, VALUE, LINENO) tuples.

       Throws ConfigError if the file is malformatted.
    """
    # List of (heading, [(key, val, lineno), ...])
    sections = []
    # [(key, val, lineno)] for the current section.
    curSection = None
    # Current line number
    lineno = 0

    # Make sure all characters in the file are ASCII.
    if not isPrintingAscii(contents):
        raise ConfigError("Invalid characters in file")

    #FFFF We should really use xreadlines or something if we have a file.
    fileLines = contents.split("\n")
    if fileLines[-1] == '':
        del fileLines[-1]

    for line in fileLines:
        lineno += 1
        if line == '':
            continue
        space = line[0] and line[0] in ' \t'
        line = line.strip()
        if line == '' or line[0] == '#':
            continue
        elif space:
            try:
                lastLine = curSection[-1]
                curSection[-1] = (lastLine[0],
                                  "%s %s" % (lastLine[1], line),lastLine[2])
            except (IndexError, TypeError):
                raise ConfigError("Unexpected indentation at line %s" %lineno)
        elif line[0] == '[':
            m = _section_re.match(line)
            curSection = [ ]
            sections.append( (m.group(1), curSection) )
        else:
            m = _entry_re.match(line)
            if not m:
                raise ConfigError("Bad entry at line %s"%lineno)
            try:
                curSection.append( (m.group(1), m.group(2), lineno) )
            except AttributeError:
                raise ConfigError("Unknown section at line %s" % lineno)

    return sections

def _readRestrictedConfigFile(contents):
    # List of (heading, [(key, val, lineno), ...])
    sections = []
    # [(key, val, lineno)] for the current section.
    curSection = None
    # Current line number
    lineno = 0

    # Make sure all characters in the file are ASCII.
    if not isPrintingAscii(contents):
        raise ConfigError("Invalid characters in file")

    fileLines = contents.split("\n")
    if fileLines[-1] == '':
        del fileLines[-1]

    for line in fileLines:
        lineno += 1
        line = line.strip()
        if line == '' or line[0] == '#':
            raise ConfigError("Empty line not allowed at line %s"%lineno)
        elif line[0] == '[':
            m = _section_re.match(line)
            if not m:
                raise ConfigError("Bad section declaration at line %s"%lineno)
            curSection = [ ]
            sections.append( (m.group(1), curSection) )
        else:
            colonIdx = line.find(':')
            if colonIdx >= 1:
                try:
                    curSection.append( (line[:colonIdx].strip(),
                                        line[colonIdx+1:].strip(), lineno) )
                except AttributeError:
                    raise ConfigError("Unknown section at line %s" % lineno)
            else:
                raise ConfigError("Bad Entry at line %s" % lineno)

    return sections

def _formatEntry(key,val,w=79,ind=4):
    """Helper function.  Given a key/value pair, returns a NL-terminated
       entry for inclusion in a configuration file, such that no line is
       avoidably longer than 'w' characters, and with continuation lines
       indented by 'ind' spaces.
    """
    ind_s = " "*(ind-1)
    if len(str(val))+len(key)+2 <= 79:
        return "%s: %s\n" % (key,val)

    lines = [  ]
    linecontents = [ "%s:" % key ]
    linelength = len(linecontents[0])
    for v in val.split(" "):
        if linelength+1+len(v) <= w:
            linecontents.append(v)
            linelength += 1+len(v)
        else:
            lines.append(" ".join(linecontents))
            linecontents = [ ind_s, v ]
            linelength = ind+len(v)
    lines.append(" ".join(linecontents))
    lines.append("") # so the last line ends with \n
    return "\n".join(lines)

class _ConfigFile:
    """Base class to parse, validate, and represent configuration files.
    """
    ##Fields:
    #  fname: Name of the underlying file.  Used by .reload()
    #  _sections: A map from secname->key->value.
    #  _sectionEntries: A  map from secname->[ (key, value) ] inorder.
    #  _sectionNames: An inorder list of secnames.
    #  _callbacks: A map from section name to a callback function that should
    #      be invoked with (section,sectionEntries) after each section is
    #      read.  This shouldn't be used for validation; it's for code that
    #      needs to change the semantics of the parser.
    #
    # Fields to be set by a subclass:
    #     _syntax is map from sec->{key:
    #                               (ALLOW/REQUIRE/ALLOW*/REQUIRE*,
    #                                 parseFn,
    #                                 default, ) }
    #     _restrictFormat is 1/0: do we allow full RFC822ness, or do
    #         we insist on a tight data format?

    ## Validation rules:
    # A key without a corresponding entry in _syntax gives an error.
    # A section without a corresponding entry is ignored.
    # ALLOW* and REQUIRE* permit multiple entries with for a given key:
    #   these entries are read into a list.
    # The magic key __SECTION__ describes whether a section is requried.
    # If parseFn is not None, it is invoked on the entry in order to
    #   get a value.  Otherwise, the value is string value of the entry.
    # If the entry is (permissibly) absent, and default is set, then
    #   the entry's value will be set to default.  Otherwise, the value
    #   will be set to None.

    _syntax = None
    _restrictFormat = 0

    def __init__(self, filename=None, string=None, assumeValid=0):
        """Create a new _ConfigFile.  If <filename> is set, read from
           a corresponding file.  If <string> is set, parse its contents.

           (If <filename> ends with ".gz", assume a file compressed
           with gzip.)

           If <assumeValid> is true, skip all unnecessary validation
           steps.  (Use this to load a file that's already been checked as
           valid.)"""
        assert filename is None or string is None
        if not hasattr(self, '_callbacks'):
            self._callbacks = {}

        self.assumeValid = assumeValid
        self.fname = filename
        if filename:
            self.reload()
        elif string:
            self.__reload(None, string)
        else:
            self.clear()

    def clear(self):
        """Remove all sections from this _ConfigFile object."""
        self._sections = {}
        self._sectionEntries = {}
        self._sectionNames = []

    def reload(self):
        """Reload this _ConfigFile object from disk.  If the object is no
           longer present and correctly formatted, raise an error, but leave
           the contents of this object unchanged."""
        if not self.fname:
            return

        contents = mixminion.Common.readPossiblyGzippedFile(self.fname)
        self.__reload(None, contents)

    def __reload(self, file, fileContents):
        """As in .reload(), but takes an open file object _or_ a string."""
        if fileContents is None:
            fileContents = file.read()
            file.close()

        fileContents = _abnormal_line_ending_re.sub("\n", fileContents)

        if self._restrictFormat:
            sections = _readRestrictedConfigFile(fileContents)
        else:
            sections = _readConfigFile(fileContents)

        # These will become self.(_sections,_sectionEntries,_sectionNames)
        # if we are successful.
        self_sections = {}
        self_sectionEntries = {}
        self_sectionNames = []
        sectionEntryLines = {}

        for secName, secEntries in sections:
            self_sectionNames.append(secName)

            if self_sections.has_key(secName):
                raise ConfigError("Duplicate section [%s]" %secName)

            section = {}
            sectionEntries = []
            entryLines = []
            self_sections[secName] = section
            self_sectionEntries[secName] = sectionEntries
            sectionEntryLines[secName] = entryLines

            secConfig = self._syntax.get(secName, None)

            if not secConfig:
                LOG.warn("Skipping unrecognized section %s", secName)
                continue

            # Set entries from the section, searching for bad entries
            # as we go.
            for k,v,line in secEntries:
                try:
                    rule, parseFn, default = secConfig[k]
                except KeyError:
                    raise ConfigError("Unrecognized key %s on line %s" %
                                      (k, line))

                # Parse and validate the value of this entry.
                if parseFn is not None:
                    try:
                        v = parseFn(v)
                    except ConfigError, e:
                        e.args = ("%s at line %s" %(e.args[0],line))
                        raise e

                sectionEntries.append( (k,v) )
                entryLines.append(line)

                # Insert the entry, checking for impermissible duplicates.
                if rule in ('REQUIRE', 'ALLOW'):
                    if section.has_key(k):
                        raise ConfigError("Duplicate entry for %s at line %s"
                                          % (k, line))
                    else:
                        section[k] = v
                else:
                    assert rule in ('REQUIRE*','ALLOW*')
                    try:
                        section[k].append(v)
                    except KeyError:
                        section[k] = [v]

            # Check for missing entries, setting defaults and detecting
            # missing requirements as we go.
            for k, (rule, parseFn, default) in secConfig.items():
                if k == '__SECTION__':
                    continue
                elif not section.has_key(k):
                    if rule in ('REQUIRE', 'REQUIRE*'):
                        raise ConfigError("Missing entry %s from section %s"
                                          % (k, secName))
                    else:
                        if parseFn is None or default is None:
                            if rule == 'ALLOW*':
                                section[k] = []
                            else:
                                section[k] = default
                        elif rule == 'ALLOW':
                            section[k] = parseFn(default)
                        else:
                            assert rule == 'ALLOW*'
                            section[k] = map(parseFn,default)

            cb = self._callbacks.get(secName, None)
            if cb:
                cb(section, sectionEntries)

        # Check for missing required sections, setting any missing
        # allowed sections to {}.
        for secName, secConfig in self._syntax.items():
            secRule = secConfig.get('__SECTION__', ('ALLOW',None,None))
            if (secRule[0] == 'REQUIRE'
                and not self_sections.has_key(secName)):
                raise ConfigError("Section [%s] not found." %secName)
            elif not self_sections.has_key(secName):
                self_sections[secName] = {}
                self_sectionEntries[secName] = []

        if not self.assumeValid:
            # Call our validation hook.
            self.validate(self_sections, self_sectionEntries,
                          sectionEntryLines, fileContents)

        self._sections = self_sections
        self._sectionEntries = self_sectionEntries
        self._sectionNames = self_sectionNames

    def _addCallback(self, section, cb):
        """For use by subclasses.  Adds a callback for a section"""
        if not hasattr(self, '_callbacks'):
            self._callbacks = {}
        self._callbacks[section] = cb

    def validate(self, sections, sectionEntries, entryLines,
                 fileContents):
        """Check additional semantic properties of a set of configuration
           data before overwriting old data.  Subclasses should override."""
        pass

    def __getitem__(self, sec):
        """self[section] -> dict

           Return a map from keys to values for a given section.  If the
           section was absent, return an empty map."""
        return self._sections[sec]

    def has_section(self, sec):
        """Return true if this config object allows a section named 'sec'."""
        return self._sections.has_key(sec)

    def getSectionItems(self, sec):
        """Return a list of ordered (key,value) tuples for a given section.
           If the section was absent, return an empty map."""
        return self._sectionEntries[sec]

    def __str__(self):
        """Returns a string configuration file equivalent to this configuration
           file."""
        lines = []
        for s in self._sectionNames:
            lines.append("[%s]\n"%s)
            for k,v in self._sectionEntries[s]:
                lines.append(_formatEntry(k,v))
            lines.append("\n")

        return "".join(lines)

class ClientConfig(_ConfigFile):
    _restrictFormat = 0
    _syntax = {
        'Host' : { '__SECTION__' : ('ALLOW', None, None),
                   'ShredCommand': ('ALLOW', _parseCommand, None),
                   'EntropySource': ('ALLOW', None, "/dev/urandom"),
                   },
        'DirectoryServers' :
                   { '__SECTION__' : ('REQUIRE', None, None),
                     'ServerURL' : ('ALLOW*', None, None),
                     'MaxSkew' : ('ALLOW', _parseInterval, "10 minutes") },
        'User' : { 'UserDir' : ('ALLOW', None, "~/.mixminion" ) },
        'Security' : { 'PathLength' : ('ALLOW', _parseInt, "8"),
                       'SURBAddress' : ('ALLOW', None, None),
                       'SURBPathLength' : ('ALLOW', _parseInt, "8"),
                       'SURBLifetime' : ('ALLOW', _parseInterval, "7 days") },
        }
    def __init__(self, fname=None, string=None):
        _ConfigFile.__init__(self, fname, string)

    def validate(self, sections, entries, lines, contents):
        _validateHostSection(sections.get('Host', {}))

        security = sections.get('Security', {})
        p = security.get('PathLength', 8)
        if not 0 < p <= 16:
            raise ConfigError("Path length must be between 1 and 16")
        if p < 4:
            LOG.warn("Your default path length is frighteningly low."
                          "  I'll trust that you know what you're doing.")

def _validateHostSection(sec):
    """Helper function: Makes sure that the shared [Host] section is correct;
       raise ConfigError if it isn't"""
    # For now, we do nothing here.  EntropySource and ShredCommand are checked
    # in configure_trng and configureShredCommand, respectively.
    pass
