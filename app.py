import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import data_loader
# ========== PostgreSQL 数据库代码（保留） ==========
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
db = SessionLocal()

# 用户信息表
class ParticipantProfile(Base):
    __tablename__ = "participant_profiles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    participant_id = Column(String, unique=True, index=True)
    participant_slot = Column(String)
    gender = Column(String)
    age_group = Column(String)
    current_residence = Column(String)
    chengdu_familiarity = Column(String)
    chongqing_familiarity = Column(String)
    professional_background = Column(String)
    travel_modes = Column(Text)
    travel_other_text = Column(Text)
    create_time = Column(DateTime, default=datetime.utcnow)

# 单题打分记录表（8个维度各存1条）
class SurveyResponse(Base):
    __tablename__ = "survey_responses"
    id = Column(Integer, primary_key=True, autoincrement=True)
    participant_id = Column(String)
    pair_id = Column(String)
    order_in_participant = Column(Integer)
    dimension = Column(String)
    dimension_order = Column(Integer)
    left_qid = Column(String)
    right_qid = Column(String)
    left_image_id = Column(String)
    right_image_id = Column(String)
    choice = Column(String)
    response_time_sec = Column(String)
    submit_time = Column(DateTime, default=datetime.utcnow)

# 首次运行自动建表
Base.metadata.create_all(bind=engine)
# ==============================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
app = Flask(__name__)
app.secret_key = 'street_view_survey_trueskill_secret_2026'

# 已全部注释删除CSV文件相关变量，不再读写本地文件
SLOT_COUNTER = 1

@app.route('/', methods=['GET', 'POST'])
def index():
    global SLOT_COUNTER
    if request.method == 'POST':
        participant_id = f"U{uuid.uuid4().hex[:6].upper()}"
        session_id = f"S{uuid.uuid4().hex[:6].upper()}"
        
        current_slot = SLOT_COUNTER
        SLOT_COUNTER += 1
        if SLOT_COUNTER > 150:
            SLOT_COUNTER = 1
            
        session['user_info'] = {
            'participant_id': participant_id,
            'session_id': session_id,
            'participant_slot': f"P_SLOT_{current_slot:03d}",
            'gender': request.form.get('gender'),
            'age_group': request.form.get('age_group'),
            'current_residence': request.form.get('current_residence'),
            'chengdu_familiarity': request.form.get('chengdu_familiarity'),
            'chongqing_familiarity': request.form.get('chongqing_familiarity'),
            'professional_background': request.form.get('professional_background'),
            'travel_walk': '1' if request.form.get('travel_walk') else '0',
            'travel_bike_ebike': '1' if request.form.get('travel_bike_ebike') else '0',
            'travel_public_transit': '1' if request.form.get('travel_public_transit') else '0',
            'travel_private_car': '1' if request.form.get('travel_private_car') else '0',
            'travel_taxi_ridehailing': '1' if request.form.get('travel_taxi_ridehailing') else '0',
            'travel_other': '1' if request.form.get('travel_other') else '0',
            'travel_other_text': request.form.get('travel_other_text', ''),
            'profile_submit_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        questions = data_loader.get_survey_questions(current_slot)
        if not questions:
            return "<h3>表格匹配失败，请检查 tables/ 目录下的 Excel/CSV 文件！</h3>"
            
        session['questions'] = questions
        session['current_index'] = 0
        
        return redirect(url_for('survey'))
        
    return render_template('index.html')

@app.route('/test')
def test_page():
    return "测试路由生效啦"
    
@app.route('/survey')
def survey():
    if 'user_info' not in session or 'questions' not in session:
        return redirect(url_for('index'))
    return render_template('survey.html')

@app.route('/get_current_question')
def get_current_question():
    if 'questions' not in session:
        return jsonify({'status': 'error', 'message': '会话不存在'})
        
    idx = session.get('current_index', 0)
    questions = session.get('questions', [])
    
    if idx >= len(questions):
        return jsonify({'status': 'completed'})
        
    return jsonify({
        'status': 'success',
        'current_index': idx,
        'total_questions': len(questions),
        'question': questions[idx]
    })

@app.route('/submit_response', methods=['POST'])
def submit_response():
    if 'user_info' not in session:
        return jsonify({'status': 'error', 'message': '登录已过期'})
        
    data = request.json or {}
    user_info = session['user_info']
    participant_id = user_info['participant_id']
    
    try:
        choices = data.get('choices')
        if not choices or not isinstance(choices, dict):
            choices = {}
            
        dimensions = ['beauty', 'safety', 'vitality', 'depression', 'wealthy', 'boring', 'harmony', 'active']
        
        try:
            start_t = datetime.fromisoformat(data['question_start_time'].replace('Z', '+00:00'))
            submit_t = datetime.fromisoformat(data['question_submit_time'].replace('Z', '+00:00'))
            response_time_sec = round((submit_t - start_t).total_seconds(), 2)
        except Exception:
            response_time_sec = 0.0

        # ========== 【核心改动1】删除全部CSV写入，循环存入数据库 ==========
        for d_idx, dim in enumerate(dimensions, start=1):
            choice = choices.get(dim, 'tie')
            record = SurveyResponse(
                participant_id=participant_id,
                pair_id=data.get('pair_id', ''),
                order_in_participant=data.get('order_in_participant', ''),
                dimension=dim,
                dimension_order=d_idx,
                left_qid=data.get('left_qid', ''),
                right_qid=data.get('right_qid', ''),
                left_image_id=data.get('left_image_id', ''),
                right_image_id=data.get('right_image_id', ''),
                choice=choice,
                response_time_sec=str(response_time_sec)
            )
            db.add(record)
        db.commit() # 一次性提交当前题8个维度记录

        # 索引自增
        new_index = session.get('current_index', 0) + 1
        session['current_index'] = new_index
        
        # ========== 【核心改动2】答完最后一题，保存用户基础信息到数据库 ==========
        if new_index >= len(session.get('questions', [])):
            # 拼接用户出行方式文本
            travel_list = []
            if user_info['travel_walk'] == '1': travel_list.append("步行")
            if user_info['travel_bike_ebike'] == '1': travel_list.append("骑行/电动车")
            if user_info['travel_public_transit'] == '1': travel_list.append("公交地铁")
            if user_info['travel_private_car'] == '1': travel_list.append("私家车")
            if user_info['travel_taxi_ridehailing'] == '1': travel_list.append("网约车/出租")
            if user_info['travel_other'] == '1': travel_list.append("其他")
            travel_text = "、".join(travel_list)

            profile = ParticipantProfile(
                participant_id=participant_id,
                participant_slot=user_info['participant_slot'],
                gender=user_info['gender'],
                age_group=user_info['age_group'],
                current_residence=user_info['current_residence'],
                chengdu_familiarity=user_info['chengdu_familiarity'],
                chongqing_familiarity=user_info['chongqing_familiarity'],
                professional_background=user_info['professional_background'],
                travel_modes=travel_text,
                travel_other_text=user_info['travel_other_text']
            )
            db.add(profile)
            db.commit()
        
        return jsonify({'status': 'success'})
        
    except Exception as e:
        db.rollback() # 出错回滚数据库，防止锁库
        print(f"【错误】写入答题记录失败: {e}")
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/thank_you')
@app.route('/submit_profile', methods=['GET', 'POST'])
def thank_you():
    if 'user_info' in session:
        session.clear()
    return """
    <div style="text-align: center; margin-top: 100px; font-family: sans-serif;">
        <h1 style="color: #34c759; font-size: 32px;">🎉 问卷全部完成！</h1>
        <p style="color: #666; font-size: 16px;">感谢您的参与，您的作答数据已永久存入数据库。</p>
    </div>
    """

# 简易后台：查看问卷数据（加密码保护，防止公开访问）
@app.route('/admin/survey_data')
def view_survey_data():
    # 访问密码，改成你自己的
    access_pwd = request.args.get("pwd", "")
    if access_pwd != "survey2026":
        return "无权访问", 403

    # 查询用户信息
    profiles = db.query(ParticipantProfile).all()
    # 查询最近200条答题记录
    responses = db.query(SurveyResponse).order_by(SurveyResponse.submit_time.desc()).limit(200).all()

    # 生成简易HTML表格
    html = """
    <style>table{border-collapse:collapse;margin:20px 0;} th,td{border:1px solid #ccc;padding:8px 12px;} h2{margin-top:30px;}</style>
    <h2>📊 用户基础信息（共 {} 人）</h2>
    <table>
        <tr><th>用户ID</th><th>槽位</th><th>性别</th><th>年龄段</th><th>提交时间</th></tr>
    """.format(len(profiles))

    for p in profiles:
        html += f"<tr><td>{p.participant_id}</td><td>{p.participant_slot}</td><td>{p.gender}</td><td>{p.age_group}</td><td>{p.create_time}</td></tr>"
    html += "</table>"

    html += f"<h2>📝 最近200条答题记录（共 {db.query(SurveyResponse).count()} 条总记录）</h2>"
    html += """
    <table>
        <tr><th>用户ID</th><th>题目ID</th><th>评价维度</th><th>选择结果</th><th>答题用时</th></tr>
    """
    for r in responses:
        html += f"<tr><td>{r.participant_id}</td><td>{r.pair_id}</td><td>{r.dimension}</td><td>{r.choice}</td><td>{r.response_time_sec}s</td></tr>"
    html += "</table>"

    return html

if __name__ == '__main__':
    app.run(debug=True, port=5000)