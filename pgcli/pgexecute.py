import logging
import psycopg2
import psycopg2.extras
import psycopg2.extensions
import sqlparse
from collections import defaultdict
from .packages import pgspecial

_logger = logging.getLogger(__name__)

# Cast all database input to unicode automatically.
# See http://initd.org/psycopg/docs/usage.html#unicode-handling for more info.
psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)

# When running a query, make pressing CTRL+C raise a KeyboardInterrupt
# See http://initd.org/psycopg/articles/2014/07/20/cancelling-postgresql-statements-python/
psycopg2.extensions.set_wait_callback(psycopg2.extras.wait_select)

def _parse_dsn(dsn, default_user, default_password, default_host,
        default_port):
    """
    This function parses a postgres url to get the different components.

    """

    user = password = host = port = dbname = None

    if dsn.startswith('postgres://'):  # Check if the string is a database url.
        dsn = dsn[len('postgres://'):]
    elif dsn.startswith('postgresql://'):
        dsn = dsn[len('postgresql://'):]

    if '/' in dsn:
        host, dbname = dsn.split('/', 1)
        if '@' in host:
            user, _, host = host.partition('@')
        if ':' in host:
            host, _, port = host.partition(':')
        if user and ':' in user:
            user, _, password = user.partition(':')

    user = user or default_user
    password = password or default_password
    host = host or default_host
    port = port or default_port
    dbname = dbname or dsn

    _logger.debug('Parsed connection params:'
            'dbname: %r, user: %r, password: %r, host: %r, port: %r',
            dbname, user, password, host, port)

    return (dbname, user, password, host, port)

class PGExecute(object):

    tables_query = '''SELECT c.relname as "Name" FROM pg_catalog.pg_class c
    LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE
    c.relkind IN ('r','') AND n.nspname <> 'pg_catalog' AND n.nspname <>
    'information_schema' AND n.nspname !~ '^pg_toast' AND
    pg_catalog.pg_table_is_visible(c.oid) ORDER BY 1;'''

    columns_query = '''SELECT table_name, column_name FROM information_schema.columns'''

    databases_query = """SELECT d.datname as "Name",
       pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
       pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
       d.datcollate as "Collate",
       d.datctype as "Ctype",
       pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
    FROM pg_catalog.pg_database d
    ORDER BY 1;"""

    def __init__(self, database, user, password, host, port):
        (self.dbname, self.user, self.password, self.host, self.port) = \
                _parse_dsn(database, default_user=user,
                        default_password=password, default_host=host,
                        default_port=port)
        self.connect()

    def connect(self, database=None, user=None, password=None, host=None,
            port=None):
        conn = psycopg2.connect(database=database or self.dbname, user=user or
                self.user, password=password or self.password, host=host or
                self.host, port=port or self.port)
        if hasattr(self, 'conn'):
            self.conn.close()
        self.conn = conn
        self.conn.autocommit = True

    def run(self, sql):
        """Execute the sql in the database and return the results. The results
        are a list of tuples. Each tuple has 3 values (rows, headers, status).
        """

        # Remove spaces and EOL
        sql = sql.strip()
        if not sql:  # Empty string
            return [(None, None, None)]

        # Remove spaces, eol and semi-colons.
        sql = sql.rstrip(';')

        # Check if the command is a \c or 'use'. This is a special exception
        # that cannot be offloaded to `pgspecial` lib. Because we have to
        # change the database connection that we're connected to.
        if sql.startswith('\c') or sql.lower().startswith('use'):
            _logger.debug('Database change command detected.')
            try:
                dbname = sql.split()[1]
            except:
                _logger.debug('Database name missing.')
                raise RuntimeError('Database name missing.')
            self.connect(database=dbname)
            self.dbname = dbname
            _logger.debug('Successfully switched to DB: %r', dbname)
            return [(None, None, 'You are now connected to database "%s" as '
                    'user "%s"' % (self.dbname, self.user))]

        try:   # Special command
            _logger.debug('Trying a pgspecial command. sql: %r', sql)
            cur = self.conn.cursor()
            return pgspecial.execute(cur, sql)
        except KeyError:  # Regular SQL
            # Split the sql into separate queries and run each one. If any
            # single query fails, the rest of them are not run and no results
            # are shown.
            queries = sqlparse.split(sql)
            return [self.execute_normal_sql(query) for query in queries]

    def execute_normal_sql(self, split_sql):
        _logger.debug('Regular sql statement. sql: %r', split_sql)
        cur = self.conn.cursor()
        cur.execute(split_sql)
        # cur.description will be None for operations that do not return
        # rows.
        if cur.description:
            headers = [x[0] for x in cur.description]
            return (cur, headers, cur.statusmessage)
        else:
            _logger.debug('No rows in result.')
            return (None, None, cur.statusmessage)

    def tables(self):
        """ Returns tuple (sorted_tables, columns). Columns is a dictionary of
            table name -> list of columns """
        columns = defaultdict(list)
        with self.conn.cursor() as cur:
            _logger.debug('Tables Query. sql: %r', self.tables_query)
            cur.execute(self.tables_query)
            tables = [x[0] for x in cur.fetchall()]

            table_set = set(tables)
            _logger.debug('Columns Query. sql: %r', self.columns_query)
            cur.execute(self.columns_query)
            for table, column in cur.fetchall():
                if table in table_set:
                    columns[table].append(column)
        return tables, columns

    def databases(self):
        with self.conn.cursor() as cur:
            _logger.debug('Databases Query. sql: %r', self.databases_query)
            cur.execute(self.databases_query)
            return [x[0] for x in cur.fetchall()]
