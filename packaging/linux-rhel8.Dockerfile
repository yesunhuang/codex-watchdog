# UBI 8.10 amd64 image digest verified against Red Hat's registry manifest.
FROM registry.access.redhat.com/ubi8/ubi@sha256:bc8b5c83e0f5a7199eeabc8ea350a65b4d9551541ad601dce95ab2442d73a912

# Keep vendor-maintained crypto and its RPM license records in the build image.
RUN yum -y install gcc make openssl-devel libffi-devel zlib-devel bzip2-devel \
      xz-devel sqlite-devel tar xz gzip git findutils binutils which \
    && yum clean all

# Official Python 3.12.14 source release and python.org SHA-256.
RUN curl --fail --location --retry 3 \
      https://www.python.org/ftp/python/3.12.14/Python-3.12.14.tar.xz \
      --output /tmp/Python-3.12.14.tar.xz \
    && echo '5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a  /tmp/Python-3.12.14.tar.xz' | sha256sum --check \
    && tar -xJf /tmp/Python-3.12.14.tar.xz -C /tmp \
    && cd /tmp/Python-3.12.14 \
    && ./configure --prefix=/opt/watchdog-python --enable-shared \
         LDFLAGS='-Wl,-rpath,/opt/watchdog-python/lib' \
    && make -j2 \
    && make install \
    && install -m 644 LICENSE /opt/watchdog-python/LICENSE.txt \
    && ln -s python3.12 /opt/watchdog-python/bin/python

ENV PATH="/opt/watchdog-python/bin:${PATH}" PYTHONUTF8=1
COPY requirements-linux-package.txt /tmp/requirements-linux-package.txt
RUN python -m pip install --disable-pip-version-check pip==26.0.1 \
    && python -m pip install --disable-pip-version-check --only-binary=:all: \
         -r /tmp/requirements-linux-package.txt \
    && python -m pip check \
    && git config --global --add safe.directory /workspace
WORKDIR /workspace
