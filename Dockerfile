FROM python:3.8

WORKDIR /var/www
COPY ./requirements.in /tmp/requirements.in
WORKDIR /tmp
RUN pip install pip==20.0.2 && pip install pip-tools && pip-compile ./requirements.in > ./requirements.txt && pip-sync ./requirements.txt

WORKDIR /var/www
