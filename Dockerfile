FROM ubuntu:18.04

ENV DEBIAN_FRONTEND noninteractive
ENV EXPORTER_BASEDIR /opt/libvirt_exporter/
ENV http_proxy=http://10.61.2.237:3128/
ENV https_proxy=http://10.61.2.237:3128/

RUN mkdir ${EXPORTER_BASEDIR}

RUN apt-get update && apt-get install -y libvirt-dev curl git gcc python3 \
    python3-pip libguestfs-dev linux-image-generic && apt-get clean all


# RUN apt-get update && apt-get install -y libvirt-bin && apt-get clean all
ADD requirements.txt ${EXPORTER_BASEDIR}/
WORKDIR ${EXPORTER_BASEDIR}
RUN pip3 install -r requirements.txt
RUN pip3 install http://download.libguestfs.org/python/guestfs-1.40.2.tar.gz

ADD libvirt_exporter.py ${EXPORTER_BASEDIR}/
CMD [ "python3", "./libvirt_exporter.py" ]
