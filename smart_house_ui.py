from flask import Flask, render_template, request, redirect, Response
from flask_login import LoginManager, UserMixin, login_user, login_required
from paho.mqtt import client as mqtt_client
import sys
import secrets
from typing import Union
from time import time
from datetime import datetime
import matplotlib.pyplot as plt
import io
import toml
import sqlite3
from pathlib import Path
import logging


# https://www.hivemq.com/blog/mqtt-essentials-part-6-mqtt-quality-of-service-levels/
QoS = 1 # 0, 1, 2


DB_PATH = Path(__file__).parent.joinpath("pressure.db")
logging.getLogger('werkzeug').setLevel(logging.ERROR)
plt.switch_backend('agg')

with open("settings.toml") as fh:
    credentials = toml.load(fh)

gTopics = {t:"" for t in credentials['topics']}
VALUES_TABLE = {
    "Control_ON": {'Включено': 0, 'Выключено': 1},
    "servo_on": {'Открыта': 0, 'Закрыта': 1}
}


app = Flask(__name__)
app.secret_key = str(secrets.token_hex())
login_manager = LoginManager()
login_manager.init_app(app)


def convert_to_ms(hours:Union[int, str], minutes:Union[int, str]) -> Union[int, Exception]:
    try:
        hours = int(hours)
        minutes = int(minutes)

        if 0 > hours > 23:
            raise Exception(f"Hours should be between [0, 23]; got: {hours}")
        if 0 > minutes > 59:
            raise Exception(f"Minutes should be between [0, 59]; got: {hours}")

        return (hours * 60 + minutes) * 1000
    except Exception as err:
        return err


def create_db_if_not_exist():
    global db_connection
    db_connection.execute('''
        CREATE TABLE IF NOT EXISTS Pressure (
            date    TEXT UNIQUE,
            value   INTEGER
        );
    ''')


def get_pressure_data_last24h() -> dict:
    """Row: date, max, avg, min"""
    global db_connection
    data = {'date': [], 'max': [], 'avg': [], 'min': []}

    try:
        cursor = db_connection.cursor()
        cursor.execute("""
            SELECT date, max(value) as v_max, round(avg(value), 1) as v_avg, min(value) as v_min
            FROM Pressure
            WHERE "date" >= datetime('now', '-23 hour')
            GROUP BY strftime('%Y-%m-%d %H', date)
            ORDER by date
        """) # '%H%M' sort by hours AND minutes

        for row in cursor:
            date_formatted = row[0].split(':')[0].split('-')[-1].replace(' ', '.')
            data['date'].append(date_formatted)
            data['max'].append(int(row[1]))
            data['avg'].append(float(row[2]))
            data['min'].append(int(row[3]))
    except Exception as err:
        print(f'parse db error: {err}')

    return data


def update_db_pressure(pressure:str):
    global db_connection
    try:
        pressure_int = int(pressure.split('.')[0])
        db_connection.execute("""INSERT INTO Pressure (date, value) VALUES (datetime(), ?);""", [pressure_int])
        db_connection.commit()
    except Exception as err:
        print(f"update db error: {err}")


def connect_mqtt() -> mqtt_client:
    def connect_callback(_client, _userdata, _flags, rc):
        print(datetime.now(), end=' ')
        if rc == 0:
            print("Connected to MQTT Broker!")
        else:
            print(f"Failed to connect: {rc}")

    def message_callback(_client, _userdata, msg):
        global gTopics
        msg_data = msg.payload.decode()
        gTopics[msg.topic] = msg_data
        if msg.topic == 'pressure_current':
            update_db_pressure(msg_data)
        if msg.topic not in ['LitrFloat', 'SumL', 'pol1', 'pressure_current']:
            print(f"{datetime.now()} received `{msg_data}` from `{msg.topic}`")

    client_mqtt = mqtt_client.Client(f"{time()}-{secrets.token_hex()}")

    client_mqtt.username_pw_set(credentials['username'], credentials['password'])
    client_mqtt.on_connect = connect_callback
    client_mqtt.on_message = message_callback
    client_mqtt.connect(credentials['broker'], credentials['port'])
    return client_mqtt


class User(UserMixin):
    def __init__(self, user_id):
        super().__init__()
        self.id = user_id


@login_manager.user_loader
def user_loader(user_id):
    if user_id != 'admin':
        return
    return User('admin')


@login_manager.unauthorized_handler
def unauthorized_handler():
    return redirect("login")


@app.route("/login", methods=["GET", "POST"])
def login():
    global USERS

    if request.method == 'GET':
        # #DEBUG
        # user = User('admin')
        # login_user(user, remember=True)
        # return redirect("/")
        # #DEBUG
        return render_template("login.html")

    password = request.form.get('password')
    if password == USERS['admin']['password']:
        user = User('admin')
        login_user(user, remember=True)
        return redirect("/")

    return """
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Неверный пароль</title>
</head>
<body><h1>Неверный пароль</h1></body>
"""


@app.route('/plot.png')
@login_required
def plot_png():
    plt.close()

    plot_data = get_pressure_data_last24h()
    if not plot_data['date']:
        return 'NO DATA'

    print(plot_data)
    x_axis = [i for i in range(len(plot_data['date']))]

    plt.plot(x_axis, plot_data['max'], marker='^', markersize=4, label="max", c='red')
    plt.plot(x_axis, plot_data['avg'], marker='.', markersize=4, label="avg", c='green')
    plt.plot(x_axis, plot_data['min'], marker='v', markersize=4, label="min", c=(0.4, 0.4, 1, 1))

    for i, x in enumerate(x_axis):
        for k in ['max', 'avg', 'min']:
            plt.annotate(
                str(plot_data[k][i]),
                xy=(x, float(plot_data[k][i])),
                xytext=(x, float(plot_data[k][i]) + (0.5 if i % 2 else -0.5)),
                fontweight=700,
                fontsize=(9 if k != 'avg' else 7),
                horizontalalignment='center',

            )

    plt.xticks(x_axis, plot_data['date'], rotation=90)
    plt.grid()

    output = io.BytesIO()
    plt.savefig(output, format='png')
    return Response(output.getvalue(), mimetype='image/png')


@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    global gTopics

    if request.method == "POST":
        print(request.form) # DEBUG
        for topic, value in request.form.items():
            if topic in VALUES_TABLE:
                value = VALUES_TABLE[topic][value]

            try:
                value = int(value)
            except:
                value = 0

            client.publish(topic, value, qos=QoS, retain=True)
            gTopics[topic] = value

            if topic == 'Control_ON' and int(value) == 1:
                client.publish("flag_plavnoe_upr", "0", qos=QoS, retain=True)
                gTopics['flag_plavnoe_upr'] = 0
                client.publish("servo_on", "1", qos=QoS, retain=True)
                gTopics['servo_on'] = 1
            elif topic == 'servo_on' and int(value) == 0:
                client.publish("flag_plavnoe_upr", "0", qos=QoS, retain=True)
                gTopics['flag_plavnoe_upr'] = 0

            print(topic, value) # DEBUG
        return redirect("/")

    else:
        print(f"current topics data: {gTopics}") # DEBUG
        return render_template('index.html', topics=list(gTopics.keys()), topics_values=list(gTopics.values()))


@app.route('/pressure')
@login_required
def sensor_pressure():
    global gTopics
    return render_template('sensor_page.html', sensor=gTopics['pressure_current'])


@app.route('/water')
@login_required
def sensor_water():
    global gTopics
    try:
        value = int(float(gTopics['LitrFloat']))
    except Exception as err:
        print(err)
        value = 0
    return render_template('sensor_page.html', sensor=value)


@app.route('/water_sum')
@login_required
def sensor_water_sum():
    global gTopics
    try:
        value = int(float(gTopics['SumL']))
    except Exception as err:
        print(err)
        value = 0
    return render_template('sensor_page.html', sensor=value)


@app.route('/servo_position')
@login_required
def sensor_servo_position():
    global gTopics
    return render_template('sensor_page.html', sensor=gTopics['pol1'])


@app.route('/water_counting')
@login_required
def water_counting():
    global gTopics
    return render_template('water_counting.html', sensor=gTopics['SumL'])


@app.route('/water_get', methods=['GET'])
@login_required
def water_get():
    return {"data": gTopics['LitrFloat']}


@app.route('/water_sum_get', methods=['GET'])
@login_required
def water_sum_get():
    return {"data": gTopics['SumL']}


@app.route('/water_sum_save/<int:waterSumCountingPoint>', methods=['POST'])
@login_required
def water_sum_save(waterSumCountingPoint):
    global gTopics
    print(f"received data from the site: {waterSumCountingPoint}")
    client.publish("savedSumL", waterSumCountingPoint, qos=QoS, retain=True)
    gTopics['savedSumL'] = waterSumCountingPoint
    return redirect('/water_sum_saved_get')


@app.route('/water_sum_saved_get', methods=['GET'])
@login_required
def water_sum_saved_get():
    global gTopics
    return {"data": gTopics['savedSumL']}


@app.route('/water_sum_reset/<string:reset>', methods=['POST'])
@login_required
def water_sum_reset(reset):
    global gTopics
    print(f"water counter need reset: {reset}")
    client.publish("resetSumL", reset, qos=QoS)
    return redirect('/water_sum_get')


@app.route('/pressure_get', methods=['GET'])
@login_required
def pressure_get():
    return {"data": gTopics['pressure_current']}


@app.route('/servo_position1_get', methods=['GET'])
@login_required
def servo_position1_get():
    return {"data": gTopics['pol1']}


@app.route('/servo_timer', methods=['POST', 'GET'])
@login_required
def sensor_servo_timer():
    global gTopics

    if request.method == "POST":
        if 'deactivate_job' in request.form:
            client.publish("flag_plavnoe_upr", "0", qos=QoS, retain=True)
            gTopics['flag_plavnoe_upr'] = 0
            return redirect("/servo_timer")

        if len(request.form) != 3:
            print(f"3 fields should be filled; current: {request.form}")
            return redirect("/servo_timer")

        ms_to_send = convert_to_ms(request.form['hours_finish'], request.form['minutes_finish'])
        if type(ms_to_send) is not int:
            print(ms_to_send)
            return redirect("/servo_timer")
        try:
            servo_finish_pos = int(request.form['servo_finish_position'])
        except:
            print(request.form['servo_finish_position'])
            return redirect("/servo_timer")

        client.publish("period_na_dv", ms_to_send, qos=QoS, retain=True)
        gTopics['period_na_dv'] = ms_to_send
        client.publish("pol2", servo_finish_pos, qos=QoS, retain=True)
        gTopics['pol2'] = servo_finish_pos

        client.publish("flag_plavnoe_upr", "1", qos=QoS, retain=True)
        gTopics['flag_plavnoe_upr'] = 1
        client.publish("Control_ON", "0", qos=QoS, retain=True)
        gTopics['Control_ON'] = 0
        client.publish("servo_on", "1", qos=QoS, retain=True)
        gTopics['servo_on'] = 1

        return redirect("/servo_timer")
    else:
        return render_template('servo_timer.html', timers=[gTopics['flag_plavnoe_upr'], gTopics['period_na_dv'], gTopics['pol2']])


if __name__ == '__main__':
    USERS = {'admin': {'password': 'secret'}}
    if len(sys.argv) == 2:
        USERS['admin']['password'] = sys.argv[1]

    db_connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    create_db_if_not_exist()

    client = connect_mqtt()
    client.loop_start()

    client.subscribe([(t, QoS) for t in credentials['topics']])

    app.run(host="0.0.0.0", ssl_context='adhoc', debug=True)

    client.disconnect()
    client.loop_stop()
