# Copyright 2002 Nick Mathewson.  See LICENSE for licensing information.
# $Id: Makefile,v 1.4 2002/08/12 18:12:24 nickm Exp $
PYTHON=python2.2

all: do_build

do_build:
	$(PYTHON) setup.py build

clean:
	$(PYTHON) setup.py clean
	rm -rf build
	rm -f lib/mixminion/_unittest.py
	rm -f lib/mixminion/*.pyc
	rm -f lib/mixminion/*.pyo
	find . -name '*~' -print0 |xargs -0 rm -f

test: do_build
	( export PYTHONPATH=.; cd build/lib*; $(PYTHON) ./mixminion/test.py )

time: do_build
	( export PYTHONPATH=.; cd build/lib*; $(PYTHON) ./mixminion/benchmark.py)

# FFFF coding style target

pychecker: do_build
	( export PYTHONPATH=.; cd build/lib*; pychecker -F ../../pycheckrc ./mixminion/*.py )

lines:
	wc -l src/*.[ch] lib/*/*.py

xxxx:
	find lib src \( -name '*.py' -or -name '*.[ch]' \) -print0 \
	   | xargs -0 grep 'XXXX\|FFFF|\?\?\?\?'