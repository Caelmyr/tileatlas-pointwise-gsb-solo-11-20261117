FROM solo12-scanweave:1.0
USER root
RUN rm -rf /app
WORKDIR /app
COPY . /app
USER 10001:10001
ENV PORT=8914 TILEATLAS_DATA_DIR=/data
CMD ["python","app.py"]
