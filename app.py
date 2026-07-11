import os
import uuid
import csv
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import data_loader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

app = Flask(__name__)
app.secret_key = 'street_view_survey_trueskill_secret_2026'

RESULTS_DIR = os.path.join(BASE_DIR, 'results')
os.makedirs(RESULTS_DIR, exist_ok=True)

PROFILE_EXPORT_FILE = os.path.join(RESULTS_DIR, 'participant_profile_export.csv')
RESPONSES_EXPORT_FILE = os.path.join(RESULTS_DIR, 'responses_export.csv')

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

        # 1. 写入答题记录
        file_exists = os.path.exists(RESPONSES_EXPORT_FILE)
        with open(RESPONSES_EXPORT_FILE, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    'response_id', 'participant_id', 'session_id', 'participant_slot',
                    'pair_id', 'order_in_participant', 'dimension', 'dimension_order',
                    'left_qid', 'right_qid', 'left_image_id', 'right_image_id',
                    'choice', 'winner_qid', 'winner_image_id', 'loser_qid', 'loser_image_id',
                    'question_start_time', 'question_submit_time', 'response_time_sec',
                    'is_tie', 'is_valid', 'created_at'
                ])
            
            for d_idx, dim in enumerate(dimensions, start=1):
                choice = choices.get(dim, 'tie')
                
                winner_qid, winner_img = '', ''
                loser_qid, loser_img = '', ''
                
                if choice == 'left':
                    winner_qid, winner_img = data.get('left_qid', ''), data.get('left_image_id', '')
                    loser_qid, loser_img = data.get('right_qid', ''), data.get('right_image_id', '')
                elif choice == 'right':
                    winner_qid, winner_img = data.get('right_qid', ''), data.get('right_image_id', '')
                    loser_qid, loser_img = data.get('left_qid', ''), data.get('left_image_id', '')
                
                is_tie = 1 if choice == 'tie' else 0
                is_valid = 1
                sub_id = f"R{uuid.uuid4().hex[:8].upper()}"
                
                writer.writerow([
                    sub_id, user_info['participant_id'], user_info['session_id'], user_info['participant_slot'],
                    data.get('pair_id', ''), data.get('order_in_participant', ''), dim, d_idx,
                    data.get('left_qid', ''), data.get('right_qid', ''), data.get('left_image_id', ''), data.get('right_image_id', ''),
                    choice, winner_qid, winner_img, loser_qid, loser_img,
                    data.get('question_start_time', ''), data.get('question_submit_time', ''), response_time_sec,
                    is_tie, is_valid, datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                ])
                
        # 索引自增
        new_index = session.get('current_index', 0) + 1
        session['current_index'] = new_index
        
        # 如果当前是最后一题提交，自动顺手持久化保存个人画像
        if new_index >= len(session.get('questions', [])):
            profile_exists = os.path.exists(PROFILE_EXPORT_FILE)
            with open(PROFILE_EXPORT_FILE, 'a', newline='', encoding='utf-8-sig') as pf:
                p_writer = csv.writer(pf)
                if not profile_exists:
                    p_writer.writerow([
                        'participant_id', 'session_id', 'participant_slot', 'gender', 'age_group',
                        'current_residence', 'chengdu_familiarity', 'chongqing_familiarity',
                        'professional_background', 'travel_walk', 'travel_bike_ebike',
                        'travel_public_transit', 'travel_private_car', 'travel_taxi_ridehailing',
                        'travel_other', 'travel_other_text', 'created_at'
                    ])
                p_writer.writerow([
                    user_info['participant_id'], user_info['session_id'], user_info['participant_slot'],
                    user_info['gender'], user_info['age_group'], user_info['current_residence'],
                    user_info['chengdu_familiarity'], user_info['chongqing_familiarity'],
                    user_info['professional_background'], user_info['travel_walk'],
                    user_info['travel_bike_ebike'], user_info['travel_public_transit'],
                    user_info['travel_private_car'], user_info['travel_taxi_ridehailing'],
                    user_info['travel_other'], user_info['travel_other_text'],
                    user_info['profile_submit_time']
                ])
        
        return jsonify({'status': 'success'})
        
    except Exception as e:
        print(f"【错误】写入答题记录失败: {e}")
        return jsonify({'status': 'error', 'message': str(e)})

# --- 核心安全路由垫片：同时监听新旧两个接口，确保完美拦截不报错 ---
@app.route('/thank_you')
@app.route('/submit_profile', methods=['GET', 'POST'])
def thank_you():
    if 'user_info' in session:
        session.clear() # 干净清除当前会话，防止脏数据影响下一次答题
    return """
    <div style="text-align: center; margin-top: 100px; font-family: sans-serif;">
        <h1 style="color: #34c759; font-size: 32px;">🎉 问卷全部完成！</h1>
        <p style="color: #666; font-size: 16px;">感谢您的参与，您的所有数据已成功且安全地落盘存入 results/ 文件夹中。</p>
    </div>
    """

if __name__ == '__main__':
    app.run(debug=True, port=5000)