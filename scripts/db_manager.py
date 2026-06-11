"""数据库管理模块：支持 Impala / StarRocks 直连查询导出/导入"""

import os
import time
import shutil
import pandas as pd
from scripts.logger import Logger


class BaseDB:
    """数据库公共基类"""

    def __init__(self, host, port, user, password, database='', log_dir=''):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.log = Logger(log_dir=log_dir)
        self.conn = None  # type: ignore
        self.cur = None  # type: ignore

    def _connect(self):
        """子类实现"""
        raise NotImplementedError

    def query(self, sql):
        """执行 SQL，返回 (results, cols)"""
        self._connect()
        if not self.cur:
            raise RuntimeError("数据库连接失败: cur is None")
        try:
            self.log.info(f"SQL: {sql}")
            self.cur.execute(sql)
            results = None
            cols = None
            if "select" in sql.lower().split()[0] if sql.strip() else False:
                results = self.cur.fetchall()
            if self.cur and self.cur.description:  # type: ignore
                cols = [i[0] for i in self.cur.description]  # type: ignore
            self.conn.commit()  # type: ignore
            self.log.info("SQL 执行成功")
            return results, cols
        except Exception as e:
            self.log.error(f"SQL 执行失败: {e} | SQL: \n{sql}")
            raise AssertionError(f"SQL 执行失败: {e} | SQL: \n{sql}")
        finally:
            if self.cur:  # type: ignore
                self.cur.close()
            if self.conn:  # type: ignore
                self.conn.close()

    def query_to_df(self, sql):
        """SQL → DataFrame（分块读取，支持大表）"""
        self._connect()
        if not self.conn:
            raise RuntimeError("数据库连接失败: conn is None")
        try:
            self.log.info(f"SQL → DataFrame: {sql}")
            chunks = []
            for chunk in pd.read_sql(sql, self.conn, chunksize=50000):
                chunks.append(chunk)
            df = pd.concat(chunks, ignore_index=True)
            self.log.info(f"DataFrame 加载完成: {len(df)} rows")
            return df
        except Exception as e:
            self.log.error(f"SQL → DataFrame 失败: {e}")
            raise AssertionError(f"SQL → DataFrame 失败: {e}")
        finally:
            if self.conn:  # type: ignore
                self.conn.close()

    def query_to_file(self, sql, file_name='', output_dir='data', file_type='parquet') -> str:
        """SQL → DataFrame → 保存文件，返回文件路径

        支持 csv 和 parquet
        本地文件已存在则直接返回，跳过 DB 查询（断点续跑场景）
        """
        os.makedirs(output_dir, exist_ok=True)
        file_name = file_name or 'query_result'
        file_name = file_name.replace('.', '_')
        file_path = os.path.abspath(os.path.join(output_dir, f"{file_name}.{file_type}"))

        if os.path.isfile(file_path):
            self.log.info(f"本地文件已存在，跳过 DB 查询: {file_path}")
            return file_path

        self.log.info("开始执行 SQL 导出")
        df = self.query_to_df(sql)
        self.log.info(f"开始写入 {file_path}")

        if file_type == 'parquet':
            df.to_parquet(file_path, index=False)
        elif file_type == 'csv':
            df.to_csv(file_path, index=False, encoding='utf-8')
        else:
            raise ValueError(f"不支持的文件类型: {file_type}，仅支持 parquet/csv")

        self.log.info(f"导出完成: {file_path} ({len(df)} rows)")
        return file_path


    def check_table_exists(self, table_name, is_raise=False):
        try:
            _, _ = self.query(f'''DESC {table_name}''')
            return True
        except Exception:
            raise_str = f'表【{table_name}】数据库中不存在 请核对表名!'
            self.log.error(raise_str)
            if is_raise:
                raise AssertionError(raise_str)
            return False


class ImpalaDB(BaseDB):
    """Impala 数据库"""

    def __init__(self, host='', port=10009, user='',
                 password='', auth_mechanism='PLAIN', database='', log_dir='', *args, **kwargs):
        super().__init__(host, port, user, password, database, log_dir=log_dir)
        self.auth_mechanism = auth_mechanism

    def _connect(self):
        """pyhive 连接"""
        try:
            from pyhive import connect
            self.conn = connect(
                host=self.host,
                port=self.port,
                auth_mechanism=self.auth_mechanism,
                user=self.user,
                password=self.password,
                database=self.database,
            )
            self.cur = self.conn.cursor()
        except Exception as e:
            raise AssertionError(f"Impala 连接失败: {e}")

    @classmethod
    def from_config(cls, config) -> "ImpalaDB":
        """从 config 构建"""
        return cls(
            host=config.db_host,
            port=config.db_port,
            user=config.db_username,
            password=config.db_password,
            database=config.db_database,
            log_dir=config.log_dir,
        )

    def _build_create_sql(self, file_path, table_name):
        """根据文件列名生成建表 SQL（全 STRING）"""
        file_ext = os.path.splitext(os.path.basename(file_path))[1]
        if file_ext == '.csv':
            df = pd.read_csv(file_path, nrows=1, encoding='utf-8')
            row_format = "ROW FORMAT DELIMITED FIELDS TERMINATED BY ','"
            stored_as = 'STORED AS TEXTFILE'
        elif file_ext == '.parquet':
            df = pd.read_parquet(file_path).head(1)
            row_format = ''
            stored_as = 'STORED AS PARQUET'
        else:
            raise AssertionError(f'不支持的文件类型: {file_ext}')
        cols_str = ',\n'.join([f'{col} STRING' for col in df.columns])
        return f'CREATE TABLE {table_name} (\n{cols_str}\n)\n{row_format}\n{stored_as}'

    def create_table_from_file(self, file_path, table_name):
        """根据文件列名自动建表（全 STRING）"""
        create_sql = self._build_create_sql(file_path, table_name)
        self.query(create_sql)
        self.log.info(f'表 {table_name} 不存在，已自动建表（全 STRING）')

    def load_from_file(self, file_path, table_name='', is_overwrite=False, is_delete_overwrite=False,is_skip_header=True,):
        """从本地文件导入到 Impala 表（支持 csv/parquet）

        流程：
        1. 表名补全（无 db 前缀则加 self.database）
        2. 表不存在则报错
        3. LOAD DATA LOCAL INPATH 导入
        """
        file_name = os.path.splitext(os.path.basename(file_path))[0]
        file_ext = os.path.splitext(os.path.basename(file_path))[1]

        # 表名补全
        if not table_name:
            table_name = file_name
        if '.' not in table_name and self.database:
            table_name = f'{self.database}.{table_name}'

        assert file_path, '必须填入文件路径'
        assert file_ext in ('.csv', '.parquet'), f'仅支持 csv/parquet 文件，当前: {file_ext}'

        # 表不存在则自动建表（全 STRING）
        if not self.check_table_exists(table_name):
            self.create_table_from_file(file_path, table_name)

        load_sql = []

        # 清除数据重写（DROP 后重建，TRUNCATE 只清数据）
        if is_delete_overwrite:
            load_sql.append(f"DROP TABLE IF EXISTS {table_name}")
            load_sql.append(self._build_create_sql(file_path, table_name))
        elif self.check_table_exists(table_name) and is_overwrite:
            load_sql.append(f"TRUNCATE TABLE {table_name}")

        # 跳过表头
        if is_skip_header:
            load_sql.append(f"ALTER TABLE {table_name} SET TBLPROPERTIES ('skip.header.line.count'='1')")

        load_sql.append(f"LOAD DATA LOCAL INPATH '{file_path}' INTO TABLE {table_name}")

        for sql in load_sql:
            self.query(sql)
        self.log.info(f'文件 {file_path} 导入成功，见表 {table_name}')


class StarRocksDB(BaseDB):
    """StarRocks 数据库"""

    def __init__(self, host='', port=19030, user='', password='',
                 database='', log_dir='', *args, **kwargs):
        super().__init__(host, port, user, password, database, log_dir=log_dir)
        self.oss_mount_path = ''
        self.s3_bucket_path = ''
        self.s3_access_key = ''
        self.s3_secret_key = ''
        self.s3_region = ''
        self.s3_endpoint = ''

    @classmethod
    def from_config(cls, config) -> "StarRocksDB":
        """从 config 构建"""
        db = cls(
            host=config.db_host,
            port=config.db_port,
            user=config.db_username,
            password=config.db_password,
            database=config.db_database,
            log_dir=config.log_dir,
        )
        db.oss_mount_path = config.oss_mount_path
        db.s3_bucket_path = config.s3_bucket_path
        db.s3_access_key = config.s3_access_key
        db.s3_secret_key = config.s3_secret_key
        db.s3_region = config.s3_region
        db.s3_endpoint = config.s3_endpoint
        return db

    def _poll_until(self, fn, wait_timeout_s=600, poll_interval=30, fail_msg=''):
        """轮询重试，直到 fn() 不抛异常或超时

        Args:
            fn: 可调用对象，成功返回值，失败抛异常
            wait_timeout_s: 超时秒数
            poll_interval: 轮询间隔秒数
            fail_msg: 超时时的错误信息前缀
        Returns:
            fn() 的返回值
        """
        max_polls = (wait_timeout_s + poll_interval - 1) // poll_interval
        time.sleep(poll_interval)
        for i in range(max_polls):
            try:
                result = fn()
                return result
            except Exception as e:
                waited = (i + 1) * poll_interval
                if i < max_polls - 1:
                    self.log.warning(f'{fail_msg or "等待重试"}，{poll_interval}s 后重试... 已等待 {waited}s / 最多 {wait_timeout_s}s，错误: {e}')
                    time.sleep(poll_interval)
                else:
                    raise

    def _connect(self):
        """pymysql 连接"""
        try:
            import pymysql
            pymysql._auth.scramble_native_password = lambda password, message: password + b"\0"
            self.conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password.encode() if isinstance(self.password, str) else self.password,
                database=self.database,
            )
            self.cur = self.conn.cursor()
        except Exception as e:
            raise AssertionError(f"StarRocks 连接失败: {e}")

    def _files_sql(self, s3_path) -> str:
        """生成 files() 函数 SQL"""
        return f'''files(
            "path" = "{s3_path}",
            "format" = "parquet",
            "compression" = "lz4",
            "single" = "true",
            "aws.s3.access_key" = "{self.s3_access_key}",
            "aws.s3.secret_key" = "{self.s3_secret_key}",
            "aws.s3.region" = "{self.s3_region}",
            "aws.s3.endpoint" = "{self.s3_endpoint}"
        )'''

    def load_from_file(self, file_path, table_name='', is_overwrite=False, is_delete_overwrite=False, wait_timeout_s=600):
        """从 parquet 文件导入到 StarRocks 表（走 S3）"""
        from datetime import datetime

        file_name = os.path.splitext(os.path.basename(file_path))[0]
        file_ext = os.path.splitext(os.path.basename(file_path))[1]

        assert file_ext == '.parquet', f'仅支持 parquet 文件，当前: {file_ext}'

        # 表名补全 / 无表名则生成临时表
        if not table_name:
            table_name = f'{self.database}.tmp_oss_load_{file_name}_{datetime.now().strftime("%Y%m%d%H%M%S")}'
            self.log.warning(f'未指定导入的表，使用临时表 {table_name}')
        elif '.' not in table_name and self.database:
            table_name = f'{self.database}.{table_name}'

        # 清除数据
        if is_delete_overwrite:
            self.query(f'DROP TABLE IF EXISTS {table_name} FORCE')
        elif self.check_table_exists(table_name) and is_overwrite:
            self.query(f'TRUNCATE TABLE {table_name}')


        s3_path = file_path.replace(self.oss_mount_path, self.s3_bucket_path)
        # 表不存在则 CTAS 建表，存在则 INSERT INTO
        if not self.check_table_exists(table_name):
            sql = f'CREATE TABLE {table_name} AS SELECT * FROM {self._files_sql(s3_path)}'
        else:
            sql = f'INSERT INTO {table_name} SELECT * FROM {self._files_sql(s3_path)}'

        # 轮询重试，等待 OSS 同步
        self._poll_until(
            lambda: self.query(sql),
            wait_timeout_s=wait_timeout_s,
            fail_msg='OSS 同步未就绪',
        )
        self.log.info(f'文件 {file_path} 导入成功，见表 {table_name}')

    def export_oss_table(self, table_name, output_dir='data', wait_timeout_s=600, where_str='', table_cols=None):
        """导出 StarRocks 表到本地 parquet 文件（走 S3 + OSS 挂载）"""
        from datetime import datetime

        table_dir = f"{table_name.replace('.', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        s3_dir = f"{self.s3_bucket_path}/{table_dir}"
        cols = ', '.join(table_cols) if table_cols else '*'
        where = f' WHERE {where_str}' if where_str else ''
        export_sql = f'INSERT INTO {self._files_sql(s3_dir + "/")} SELECT {cols} FROM {table_name}{where}'
        self.query(export_sql)

        # 轮询等待 OSS 挂载同步
        local_mount_dir = os.path.join(self.oss_mount_path, table_dir)

        def _check_file_ready():
            if os.path.isdir(local_mount_dir) and os.listdir(local_mount_dir):
                local_file = os.path.join(local_mount_dir, os.listdir(local_mount_dir)[0])
                self.log.info(f'OSS 同步完成，获取到文件: {local_file}')
                return local_file
            raise FileNotFoundError(f'OSS 挂载目录无文件: {local_mount_dir}')

        local_file = self._poll_until(
            _check_file_ready,
            wait_timeout_s=wait_timeout_s,
            fail_msg='等待 OSS 同步',
        )

        # 复制到输出目录
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{table_dir}.parquet")
        shutil.copy2(str(local_file), output_path)
        self.log.info(f'表 {table_name} 导出成功: {output_path}')
        return output_path
