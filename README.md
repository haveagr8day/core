CORE
====

CORE: Common Open Research Emulator

Copyright (c)2005-2018 the Boeing Company.

See the LICENSE file included in this distribution.

About
-----

CORE is a tool for emulating networks using a GUI or Python scripts. The CORE
project site (1) is a good source of introductory information, with a manual,
screenshots, and demos about this software. The GitHub project (2) hosts the
source repos, wiki, and bug tracker. There is a deprecated
Google Code page (3) with the old wiki, blog, bug tracker, and quickstart guide.

1. http://www.nrl.navy.mil/itd/ncs/products/core
1. https://github.com/coreemu/core
1. http://code.google.com/p/coreemu/
1. [Official Documentation](https://downloads.pf.itd.nrl.navy.mil/docs/core/core-html/index.html)

Building CORE
-------------

To build this software you should use:

```shell
./bootstrap.sh
./configure
make
sudo make install
```

Note: You may need to pass the proxy settings to sudo make install:
    sudo make install HTTP_PROXY=<proxy>

Here is what is installed with 'make install':

    /usr/local/bin/core-gui
    /usr/local/bin/core-daemon
    /usr/local/bin/[vcmd, vnoded, coresendmsg, core-cleanup.sh]
    /usr/local/lib/core/*
    /usr/local/share/core/*
    /usr/local/lib/python2.6/dist-packages/core/*
    /usr/local/lib/python2.6/dist-packages/[netns,vcmd].so
    /etc/core/*
    /etc/init.d/core

See the manual for the software required for building CORE.

Building Documentation
----------------------

Being able to build documentation depends on help2man being installed.

Once that has been done you can run the following commands:

```shell
./bootstrap.sh
./configure
make html
```

Building Packages
-----------------

Install fpm

    http://fpm.readthedocs.io/en/latest/installing.html

Build package commands, DESTDIR is used for gui packaging only

```shell
./bootstrap.sh
./configure
make
mkdir /tmp/core-gui
make fpm DESTDIR=/tmp/core-gui
```

This will produce:

* CORE GUI rpm/deb files
* CORE ns3 rpm/deb files
* CORE python rpm/deb files for SysV and systemd service types

Running CORE
------------

First start the CORE services:

```shell
sudo /etc/init.d/core-daemon start
```

This automatically runs the core-daemon program.
Assuming the GUI is in your PATH, run the CORE GUI by typing the following:

```shell
core-gui
```

This launches the CORE GUI. You do not need to run the GUI as root.


Support
-------

If you have questions, comments, or trouble, please use the CORE mailing lists:

- [core-users](https://pf.itd.nrl.navy.mil/mailman/listinfo/core-users) for general comments and questions
- [core-dev](https://pf.itd.nrl.navy.mil/mailman/listinfo/core-dev) for bugs, compile errors, and other development issues
