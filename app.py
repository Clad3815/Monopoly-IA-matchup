"""
Application Flask pour Monopoly Manager
"""

import os
import json
import subprocess
import threading
import time
import sys
import importlib.util
import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory
import config
from src.game.monopoly import MonopolyGame
from src.game.contexte import Contexte
from src.game.listeners import MonopolyListeners
from src.core.game_loader import GameLoader
from services.event_bus import EventBus, EventTypes
from services.popup_service import PopupService
from services.ai_service import AIService
from services.auto_start_manager import AutoStartManager
from services.health_check_service import HealthCheckService
from api.popup_endpoints import create_popup_blueprint

app = Flask(__name__)

# Initialiser l'Event Bus et les services
event_bus = EventBus(app)
popup_service = PopupService(event_bus)
ai_service = AIService(event_bus)
auto_start_manager = AutoStartManager(config, event_bus)
health_check_service = HealthCheckService()

# Enregistrer les blueprints
app.register_blueprint(create_popup_blueprint(popup_service))

# Variables globales pour le jeu
game = None
contexte = None
dolphin_process = None
terminal_output = []
terminal_lock = threading.Lock()
ai_process = None
ai_script = None
system_logs = []
logs_lock = threading.Lock()
monitor_process = None

def check_dolphin_status():
    """Vérifie périodiquement si Dolphin est toujours en cours d'exécution"""
    global dolphin_process, game, contexte
    
    while True:
        time.sleep(2)  # Vérifier toutes les 2 secondes
        if dolphin_process:
            # Vérifier si le processus est toujours actif
            if dolphin_process.poll() is not None:
                # Dolphin s'est fermé
                add_log("Dolphin s'est fermé de manière inattendue", "warning")
                dolphin_process = None
                game = None
                contexte = None
                
                # Nettoyer les processus associés
                try:
                    subprocess.run(['taskkill', '/F', '/IM', 'DolphinMemoryEngine.exe'], 
                                 creationflags=subprocess.CREATE_NO_WINDOW,
                                 stderr=subprocess.PIPE,
                                 stdout=subprocess.PIPE)
                except:
                    pass

def initialize_game():
    """Initialise le jeu Monopoly et le contexte en utilisant le code existant dans main.py"""
    global game, contexte
    
    try:
        # Charger le module main.py dynamiquement
        spec = importlib.util.spec_from_file_location("main", "main.py")
        main_module = importlib.util.module_from_spec(spec)
        sys.modules["main"] = main_module
        spec.loader.exec_module(main_module)
        
        # Initialiser le GameLoader avec les fichiers manifeste et de sauvegarde
        manifest_path = os.path.join(config.WORKSPACE_DIR, "game_files", "starting_state.jsonc")
        save_path = config.SAVE_FILE_PATH
        
        # Vérifier que les fichiers existent
        if not os.path.exists(manifest_path):
            print(f"Erreur: Le fichier manifeste {manifest_path} n'existe pas")
            return None, None
        
        if not os.path.exists(save_path):
            print(f"Erreur: Le fichier de sauvegarde {save_path} n'existe pas")
            return None, None
        
        # Créer le GameLoader
        data = GameLoader(manifest_path, save_path)
        
        # Créer une instance du jeu
        game = MonopolyGame(data)
        
        # Créer les listeners
        events = MonopolyListeners(game)
        events.tps = 30
        events.interval_player = .1
        
        # Enregistrer les callbacks depuis main.py
        events.on("player_added", main_module.on_player_added)
        events.on("player_removed", main_module.on_player_removed)
        events.on("player_money_changed", main_module.on_player_money_changed)
        events.on("player_name_changed", main_module.on_player_name_changed)
        events.on("player_dice_changed", main_module.on_player_dice_changed)
        events.on("player_goto_changed", main_module.on_player_goto_changed)
        events.on("message_added", main_module.on_message_added)
        events.on("message_removed", main_module.on_message_removed)
        events.on("*", main_module.on_event)
        
        # Initialiser le contexte
        contexte = Contexte(game, events)
        print("📊 Contexte initialisé et prêt à enregistrer les événements")
        
        # Démarrer les listeners pour capturer les événements
        events.start()
        
        return game, contexte
    except Exception as e:
        print(f"Erreur lors de l'initialisation du jeu: {e}")
        import traceback
        traceback.print_exc()
        # Créer des objets vides pour éviter les erreurs
        game = None
        contexte = None
        return None, None

def capture_terminal_output():
    """Capture la sortie du terminal dans une liste circulaire"""
    global terminal_output
    
    # Rediriger stdout vers notre buffer
    original_stdout = sys.stdout
    
    class StdoutRedirector:
        def write(self, text):
            global terminal_output
            if text.strip():  # Ignorer les lignes vides
                with terminal_lock:
                    # Stocker le texte brut avec les emojis
                    terminal_output.append(text.strip())
                    # Garder seulement les 100 dernières lignes
                    if len(terminal_output) > 100:
                        terminal_output = terminal_output[-100:]
            original_stdout.write(text)
        
        def flush(self):
            original_stdout.flush()
    
    sys.stdout = StdoutRedirector()
    
    while True:
        # Capturer la sortie du processus Dolphin
        if dolphin_process:
            try:
                output = dolphin_process.stdout.readline()
                if output and output.strip():  # Ignorer les lignes vides
                    with terminal_lock:
                        terminal_output.append(output.strip())
                        # Garder seulement les 100 dernières lignes
                        if len(terminal_output) > 100:
                            terminal_output = terminal_output[-100:]
            except:
                pass
        time.sleep(0.1)

def cleanup_existing_processes():
    """Nettoie les processus Dolphin et Memory Engine existants"""
    try:
        # Tuer tous les processus Dolphin.exe
        subprocess.run(['taskkill', '/F', '/IM', 'Dolphin.exe'], 
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     stderr=subprocess.PIPE,
                     stdout=subprocess.PIPE)
    except:
        pass  # Ignorer si aucun processus Dolphin n'est en cours
        
    try:
        # Tuer tous les processus DolphinMemoryEngine.exe
        subprocess.run(['taskkill', '/F', '/IM', 'DolphinMemoryEngine.exe'], 
                     creationflags=subprocess.CREATE_NO_WINDOW,
                     stderr=subprocess.PIPE,
                     stdout=subprocess.PIPE)
    except:
        pass  # Ignorer si aucun processus Memory Engine n'est en cours

# Routes Flask
@app.route('/')
def index():
    """Page d'accueil"""
    return render_template('index.html', refresh_interval=config.REFRESH_INTERVAL)

@app.route('/admin')
def admin():
    """Page d'administration"""
    return render_template('admin.html')

@app.route('/monitoring')
def monitoring():
    """Page de monitoring centralisé"""
    return render_template('monitoring.html')

@app.route('/static/<path:path>')
def send_static(path):
    """Sert les fichiers statiques"""
    return send_from_directory('static', path)

@app.route('/api/context')
def get_context():
    """Renvoie le contexte actuel du jeu"""
    try:
        # Si Dolphin n'est pas en cours d'exécution, renvoyer un contexte initial
        if not dolphin_process or dolphin_process.poll() is not None:
            return jsonify({
                "global": {
                    "status": "stopped",
                    "message": "Dolphin n'est pas en cours d'exécution",
                    "properties": [],
                    "current_turn": 0,
                    "player_count": 0,
                    "player_names": []
                },
                "events": [],
                "players": {},
                "board": {
                    "spaces": []
                }
            })
            
        # Si le jeu n'est pas initialisé, renvoyer un contexte d'attente
        if not game or not contexte:
            return jsonify({
                "global": {
                    "status": "starting",
                    "message": "Le jeu est en cours d'initialisation",
                    "properties": [],
                    "current_turn": 0,
                    "player_count": 0,
                    "player_names": []
                },
                "events": [],
                "players": {},
                "board": {
                    "spaces": []
                }
            })
            
        if os.path.exists(config.CONTEXT_FILE):
            with open(config.CONTEXT_FILE, 'r', encoding='utf-8') as f:
                context_data = json.load(f)
                context_data["global"]["status"] = "running"
                context_data["global"]["message"] = "Le jeu est en cours d'exécution"
                return jsonify(context_data)
        else:
            return jsonify({
                "global": {
                    "status": "error",
                    "message": "Le fichier de contexte n'existe pas encore",
                    "properties": [],
                    "current_turn": 0,
                    "player_count": 0,
                    "player_names": []
                },
                "events": [],
                "players": {},
                "board": {
                    "spaces": []
                }
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/terminal')
def get_terminal():
    """Renvoie les dernières lignes de sortie du terminal"""
    with terminal_lock:
        return jsonify(terminal_output)

@app.route('/api/logs')
def get_logs():
    """Renvoie les logs du système"""
    with logs_lock:
        return jsonify(system_logs)

def add_log(message, log_type='info'):
    """Ajoute un log au système"""
    with logs_lock:
        log_entry = {
            'timestamp': datetime.datetime.now().strftime('%H:%M:%S'),
            'message': message,
            'type': log_type
        }
        system_logs.append(log_entry)
        # Garder seulement les 1000 derniers logs
        if len(system_logs) > 1000:
            system_logs.pop(0)
        print(f"[{log_type.upper()}] {message}")

@app.route('/api/players', methods=['GET', 'POST'])
def manage_players():
    """Gère les informations des joueurs"""
    global game
    
    # Vérifier si Dolphin est en cours d'exécution
    if not dolphin_process or dolphin_process.poll() is not None:
        return jsonify({"error": "Dolphin n'est pas en cours d'exécution"}), 503
        
    # Vérifier si le jeu est initialisé
    if not game:
        return jsonify({"error": "Le jeu n'est pas initialisé"}), 503
    
    if request.method == 'GET':
        try:
            players = []
            for p in game.players:
                players.append({
                    'id': p.id,
                    'name': p.name,
                    'money': p.money,
                    'position': p.position
                })
            return jsonify(players)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    if request.method == 'POST':
        try:
            data = request.json
            player_id = data.get('id')
            new_name = data.get('name')
            new_money = data.get('money')
            
            # Mettre à jour le joueur
            for p in game.players:
                if p.id == player_id:
                    if new_name:
                        p.name = new_name
                    if new_money is not None:
                        p.money = int(new_money)
            
            # Mettre à jour le contexte
            if contexte is not None:
                contexte._update_context()
                contexte._save_context()
            
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route('/api/dolphin', methods=['POST', 'DELETE'])
def manage_dolphin():
    """Gère le démarrage et l'arrêt de Dolphin"""
    global dolphin_process, game, contexte, monitor_process
    
    if request.method == 'POST':
        try:
            print("Démarrage de Dolphin et de tous les systèmes...")
            add_log("Démarrage de tous les systèmes...", "info")
            
            # Ne pas lancer les systèmes ici - on attend que Dolphin soit démarré
            
            # Nettoyer les processus existants
            print("Nettoyage des processus existants...")
            cleanup_existing_processes()
            
            # Vérifier que les fichiers existent
            print(f"Vérification des chemins...")
            if not os.path.exists(config.DOLPHIN_PATH):
                return jsonify({"error": f"Dolphin.exe introuvable: {config.DOLPHIN_PATH}"}), 500
                
            if not os.path.exists(config.MONOPOLY_ISO_PATH):
                return jsonify({"error": f"Fichier ISO introuvable: {config.MONOPOLY_ISO_PATH}"}), 500
            
            # Démarrer Dolphin avec le jeu et la sauvegarde
            print(f"Lancement de Dolphin: {config.DOLPHIN_PATH}")
            try:
                # Vérifier que le fichier de sauvegarde existe
                if not os.path.exists(config.SAVE_FILE_PATH):
                    print(f"ATTENTION: Fichier de sauvegarde introuvable: {config.SAVE_FILE_PATH}")
                
                # Lancement de Dolphin avec la sauvegarde et résolution plus grande
                # Note: Dolphin utilise différents paramètres selon les versions
                # -s : pour charger un état de sauvegarde (state)
                # -l : pour charger un fichier de sauvegarde (load)
                dolphin_cmd = [
                    config.DOLPHIN_PATH,
                    '-b',
                    '-e', config.MONOPOLY_ISO_PATH,
                    '-s', config.SAVE_FILE_PATH,
                    '-C', 'Dolphin.Display.Fullscreen=False',
                    '-C', 'Dolphin.Display.RenderWindowWidth=1280',
                    '-C', 'Dolphin.Display.RenderWindowHeight=720',
                    '-C', 'GFX.Settings.EFBScale=2',
                    '-C', 'GFX.Settings.InternalResolution=2',
                    '-C', 'GFX.Enhancements.InternalResolution=2'  # Autre nom possible
                ]
                
                print(f"Commande: {' '.join(dolphin_cmd)}")
                dolphin_process = subprocess.Popen(
                    dolphin_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                
                print(f"Dolphin démarré avec PID: {dolphin_process.pid}")
            except Exception as e:
                print(f"Erreur lors du lancement de Dolphin: {str(e)}")
                return jsonify({"error": f"Erreur lors du lancement de Dolphin: {str(e)}"}), 500
            
            # Attendre un peu que Dolphin démarre
            print("Attente du démarrage de Dolphin...")
            time.sleep(3)
            
            # Lancer Dolphin Memory Engine
            if os.path.exists(config.DOLPHIN_MEMORY_ENGINE_PATH):
                print(f"Lancement de Dolphin Memory Engine: {config.DOLPHIN_MEMORY_ENGINE_PATH}")
                try:
                    # Lancement simplifié de DME
                    dme_cmd = [config.DOLPHIN_MEMORY_ENGINE_PATH]
                    
                    print(f"Commande: {' '.join(dme_cmd)}")
                    if sys.platform == 'win32':
                        dme_process = subprocess.Popen(
                            dme_cmd,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    else:
                        dme_process = subprocess.Popen(
                            dme_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE
                        )
                    
                    print(f"Dolphin Memory Engine démarré avec PID: {dme_process.pid}")
                except Exception as e:
                    print(f"Erreur lors du lancement de Dolphin Memory Engine: {str(e)}")
                    print("Continuons sans Dolphin Memory Engine...")
            else:
                print("Dolphin Memory Engine introuvable, continuons sans...")
            
            # Attendre que Memory Engine se connecte
            print("Attente de la connexion de Memory Engine...")
            time.sleep(3)
            
            # Initialiser le jeu
            print("Initialisation du jeu...")
            try:
                game, contexte = initialize_game()
                
                if game and contexte:
                    print("Jeu initialisé avec succès")
                    add_log("Jeu initialisé avec succès", "success")
                    
                    # Maintenant démarrer tous les systèmes auxiliaires
                    def on_systems_ready(success, message):
                        global monitor_process
                        if success:
                            add_log("✅ Tous les systèmes auxiliaires démarrés", "success")
                            monitor_process = auto_start_manager.processes.get('monitor')
                            print(f"Monitor process: {monitor_process}")
                        else:
                            add_log(f"❌ Erreur démarrage systèmes: {message}", "error")
                            print(f"Erreur démarrage systèmes: {message}")
                    
                    # Lancer les systèmes en arrière-plan
                    print("🚀 Lancement des systèmes auxiliaires...")
                    auto_start_manager.start_all_systems(callback=on_systems_ready)
                    
                    return jsonify({"success": True, "message": "Dolphin started and all systems launching"})
                else:
                    print("Échec de l'initialisation du jeu")
                    return jsonify({"error": "Failed to initialize game"}), 500
            except Exception as e:
                print(f"Erreur lors de l'initialisation du jeu: {str(e)}")
                return jsonify({"error": f"Erreur lors de l'initialisation du jeu: {str(e)}"}), 500
                
        except Exception as e:
            print(f"Erreur générale lors du démarrage de Dolphin: {str(e)}")
            return jsonify({"error": str(e)}), 500
            
    elif request.method == 'DELETE':
        try:
            if dolphin_process:
                print("Arrêt de tous les systèmes...")
                add_log("Arrêt de tous les systèmes...", "info")
                
                # Utiliser AutoStartManager pour arrêter tous les systèmes
                auto_start_manager.stop_all_systems()
                
                # Arrêter Dolphin et Memory Engine
                cleanup_existing_processes()
                dolphin_process = None
                game = None
                contexte = None
                monitor_process = None
                
                print("Tous les systèmes arrêtés avec succès")
                return jsonify({"success": True, "message": "All systems stopped successfully"})
            else:
                print("Dolphin n'est pas en cours d'exécution")
                return jsonify({"error": "Dolphin is not running"}), 404
                
        except Exception as e:
            print(f"Erreur lors de l'arrêt de Dolphin: {str(e)}")
            return jsonify({"error": str(e)}), 500

@app.route('/api/restart', methods=['POST'])
def restart_game():
    """Redémarre le jeu Monopoly"""
    global game, contexte
    
    try:
        # Réinitialiser le jeu
        game, contexte = initialize_game()
        
        if game is None:
            return jsonify({'success': False, 'error': 'Impossible d\'initialiser le jeu. Vérifiez les fichiers manifeste et de sauvegarde.'}), 500
        
        # Redémarrer Dolphin si demandé
        restart_dolphin = request.json.get('restart_dolphin', False)
        if restart_dolphin:
            return manage_dolphin()
        
        return jsonify({'success': True, 'message': 'Jeu redémarré'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/config', methods=['GET', 'POST'])
def manage_config():
    """Gère la configuration de l'application"""
    if request.method == 'GET':
        # Renvoyer la configuration actuelle avec les valeurs par défaut
        config_data = {
            'dolphinPath': config.DOLPHIN_PATH,
            'isoPath': config.MONOPOLY_ISO_PATH,
            'savePath': config.SAVE_FILE_PATH,
            'memoryEnginePath': config.DOLPHIN_MEMORY_ENGINE_PATH,
            'refreshInterval': config.REFRESH_INTERVAL
        }
        return jsonify(config_data)
    
    if request.method == 'POST':
        try:
            # Mettre à jour la configuration
            data = request.json
            
            # Mettre à jour les variables de configuration
            if 'dolphin_path' in data:
                config.DOLPHIN_PATH = data['dolphin_path']
            if 'monopoly_iso_path' in data:
                config.MONOPOLY_ISO_PATH = data['monopoly_iso_path']
            if 'save_file_path' in data:
                config.SAVE_FILE_PATH = data['save_file_path']
            if 'memory_engine_path' in data:
                config.DOLPHIN_MEMORY_ENGINE_PATH = data['memory_engine_path']
            if 'refresh_interval' in data:
                config.REFRESH_INTERVAL = int(data['refresh_interval'])
            
            # Sauvegarder la configuration dans le dossier config
            with open(config.USER_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            return jsonify({'success': True, 'message': 'Configuration updated successfully'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@app.route('/api/dolphin/status')
def get_dolphin_status():
    """Renvoie l'état actuel de Dolphin"""
    try:
        is_running = dolphin_process is not None and dolphin_process.poll() is None
        is_game_initialized = game is not None
        
        return jsonify({
            'running': is_running,
            'game_initialized': is_game_initialized,
            'message': 'Dolphin is running and game is initialized' if is_running and is_game_initialized
                      else 'Dolphin is running' if is_running
                      else 'Dolphin is not running'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/omniparser/status')
def get_omniparser_status():
    """Renvoie l'état actuel d'OmniParser"""
    try:
        # Vérifier si OmniParser est accessible
        try:
            import urllib.request
            response = urllib.request.urlopen('http://localhost:8000/probe/', timeout=2)
            is_running = response.status == 200
        except:
            is_running = False
            
        return jsonify({
            'running': is_running,
            'url': 'http://localhost:8000'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/omniparser', methods=['POST', 'DELETE'])
def manage_omniparser():
    """Gère le démarrage et l'arrêt d'OmniParser"""
    if request.method == 'POST':
        try:
            if sys.platform == 'win32':
                # Windows : utiliser le script batch qui lance OmniParser + monitoring
                script_path = os.path.join(config.WORKSPACE_DIR, 'start_omniparser_with_monitor.bat')
                cmd = f'start "OmniParser avec Monitoring" cmd /k "{script_path}"'
                subprocess.Popen(cmd, shell=True)
            else:
                # Linux/Mac : ouvrir un nouveau terminal
                omniparser_dir = os.path.join(config.WORKSPACE_DIR, 'omniparserserver')
                subprocess.Popen(['gnome-terminal', '--', 'bash', '-c', 
                                f'cd {omniparser_dir} && docker compose up & sleep 30 && cd {config.WORKSPACE_DIR} && python monitor_popups.py; read'], 
                               cwd=omniparser_dir)
            
            add_log('OmniParser démarré avec monitoring automatique', 'success')
            return jsonify({'success': True, 'message': 'OmniParser démarré avec monitoring'})
        except Exception as e:
            add_log(f'Erreur lors du démarrage d\'OmniParser: {str(e)}', 'error')
            return jsonify({'error': str(e)}), 500
    
    elif request.method == 'DELETE':
        try:
            # Arrêter OmniParser
            omniparser_dir = os.path.join(config.WORKSPACE_DIR, 'omniparserserver')
            subprocess.Popen(['docker compose', 'down'], cwd=omniparser_dir)
            add_log('OmniParser arrêté', 'info')
            return jsonify({'success': True, 'message': 'OmniParser arrêté'})
        except Exception as e:
            add_log(f'Erreur lors de l\'arrêt d\'OmniParser: {str(e)}', 'error')
            return jsonify({'error': str(e)}), 500

@app.route('/api/ai/status')
def get_ai_status():
    """Renvoie l'état actuel des AI Actions"""
    global ai_process
    try:
        is_running = ai_process is not None and ai_process.poll() is None
        return jsonify({
            'running': is_running,
            'script': ai_script if is_running else None
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ai', methods=['POST', 'DELETE'])
def manage_ai():
    """Gère le démarrage et l'arrêt des AI Actions"""
    global ai_process, ai_script
    
    if request.method == 'POST':
        try:
            data = request.json
            script = data.get('script', 'test_search_popup.py')
            
            # Vérifier que le script existe
            script_path = os.path.join(config.WORKSPACE_DIR, script)
            if not os.path.exists(script_path):
                add_log(f'Script {script} introuvable', 'error')
                return jsonify({'error': f'Script {script} introuvable'}), 404
            
            # Démarrer le script AI dans un nouveau terminal
            if sys.platform == 'win32':
                # Windows : ouvrir un nouveau terminal cmd
                cmd = f'start "AI Actions - {script}" cmd /k "cd /d {config.WORKSPACE_DIR} && python {script}"'
                ai_process = subprocess.Popen(cmd, shell=True)
            else:
                # Linux/Mac : ouvrir un nouveau terminal
                ai_process = subprocess.Popen(['gnome-terminal', '--', 'bash', '-c', 
                                             f'cd {config.WORKSPACE_DIR} && python {script}; read'])
            
            ai_script = script
            add_log(f'AI Actions démarré avec {script} dans un nouveau terminal', 'success')
            
            return jsonify({'success': True, 'message': f'AI Actions démarré avec {script}'})
        except Exception as e:
            add_log(f'Erreur lors du démarrage des AI Actions: {str(e)}', 'error')
            return jsonify({'error': str(e)}), 500
    
    elif request.method == 'DELETE':
        try:
            if ai_process:
                ai_process.terminate()
                ai_process = None
                ai_script = None
                add_log('AI Actions arrêté', 'info')
                return jsonify({'success': True, 'message': 'AI Actions arrêté'})
            else:
                return jsonify({'error': 'AI Actions n\'est pas en cours d\'exécution'}), 404
        except Exception as e:
            add_log(f'Erreur lors de l\'arrêt des AI Actions: {str(e)}', 'error')
            return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/start', methods=['POST'])
def start_monitor():
    """Démarre le monitor centralisé"""
    global monitor_process
    
    try:
        if monitor_process and monitor_process.poll() is None:
            return jsonify({'error': 'Monitor already running'}), 400
        
        # Lancer le monitor centralisé
        monitor_script = os.path.join(config.WORKSPACE_DIR, 'monitor_centralized.py')
        
        if sys.platform == 'win32':
            cmd = f'start "Monopoly Monitor" cmd /k "python {monitor_script}"'
            monitor_process = subprocess.Popen(cmd, shell=True)
        else:
            monitor_process = subprocess.Popen(['python', monitor_script])
        
        add_log('Monitor centralisé démarré', 'success')
        
        # Publier l'événement
        event_bus.publish(EventTypes.SERVICE_STARTED, {
            'service': 'monitor',
            'pid': monitor_process.pid
        })
        
        return jsonify({'success': True, 'message': 'Monitor started'})
        
    except Exception as e:
        add_log(f'Erreur démarrage monitor: {str(e)}', 'error')
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/stop', methods=['POST'])
def stop_monitor():
    """Arrête le monitor centralisé"""
    global monitor_process
    
    try:
        if monitor_process:
            monitor_process.terminate()
            monitor_process = None
            add_log('Monitor arrêté', 'info')
            
            event_bus.publish(EventTypes.SERVICE_STOPPED, {
                'service': 'monitor'
            })
            
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Monitor not running'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/status')
def get_monitor_status():
    """Renvoie le statut du monitor"""
    try:
        is_running = monitor_process is not None and monitor_process.poll() is None
        return jsonify({
            'running': is_running,
            'pid': monitor_process.pid if is_running else None
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ai/service/status')
def get_ai_service_status():
    """Renvoie le statut du service IA interne"""
    return jsonify({
        'available': ai_service.available,
        'model': 'gpt-4o-mini' if ai_service.available else None
    })

@app.route('/api/calibration/status')
def get_calibration_status():
    """Vérifie le statut de la calibration"""
    try:
        import subprocess
        result = subprocess.run(['python', 'check_calibration.py'], 
                              capture_output=True, text=True)
        
        is_valid = result.returncode == 0
        
        # Lire les infos de calibration si disponible
        calibration_info = None
        calibration_file = os.path.join(config.WORKSPACE_DIR, 'game_files', 'calibration.json')
        if os.path.exists(calibration_file):
            try:
                with open(calibration_file, 'r') as f:
                    import json
                    calibration_info = json.load(f)
            except:
                pass
        
        return jsonify({
            'valid': is_valid,
            'message': result.stdout.strip() if result.stdout else 'Unknown status',
            'calibration_info': calibration_info
        })
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)}), 500

@app.route('/api/calibration', methods=['POST'])
def start_calibration():
    """Lance le processus de calibration"""
    try:
        # Choisir le script selon si Dolphin est en cours ou non
        is_dolphin_running = dolphin_process is not None and dolphin_process.poll() is None
        
        if is_dolphin_running:
            # Si Dolphin tourne déjà, utiliser l'ancienne méthode
            calibration_script = os.path.join(config.WORKSPACE_DIR, 'run_calibration.py')
            script_name = 'Calibration (Dolphin déjà ouvert)'
        else:
            # Sinon, utiliser le nouveau script qui lance Dolphin
            calibration_script = os.path.join(config.WORKSPACE_DIR, 'run_calibration_with_dolphin.py')
            script_name = 'Calibration avec Dolphin'
        
        if not os.path.exists(calibration_script):
            return jsonify({'error': 'Script de calibration introuvable'}), 404
        
        # Lancer la calibration dans un nouveau terminal
        if sys.platform == 'win32':
            cmd = f'start "{script_name}" cmd /k "python {calibration_script}"'
            subprocess.Popen(cmd, shell=True)
        else:
            subprocess.Popen(['gnome-terminal', '--', 'python', calibration_script])
        
        message = f'{script_name} lancée dans un nouveau terminal'
        add_log(message, 'info')
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        add_log(f'Erreur lors du lancement de la calibration: {str(e)}', 'error')
        return jsonify({'error': str(e)}), 500

@app.route('/api/create_demo_image')
def create_demo_image():
    """Crée une image de démonstration pour Dolphin"""
    try:
        # Créer un dossier static/img s'il n'existe pas
        img_dir = os.path.join('static', 'img')
        if not os.path.exists(img_dir):
            os.makedirs(img_dir)
        
        # Chemin de l'image de démonstration
        demo_img_path = os.path.join(img_dir, 'dolphin_demo.png')
        
        # Créer une image simple avec du texte
        from PIL import Image, ImageDraw
        
        # Créer une image noire
        img = Image.new('RGB', (800, 600), color=(0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Ajouter du texte
        draw.text((400, 300), "Dolphin Emulator", fill=(255, 255, 255))
        draw.text((400, 330), "Running Monopoly", fill=(255, 255, 255))
        
        # Sauvegarder l'image
        img.save(demo_img_path)
        
        return jsonify({'success': True, 'message': 'Demo image created successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/health')
def health_check():
    """Endpoint pour vérifier la santé du système"""
    try:
        status = health_check_service.get_system_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/health/check', methods=['POST'])
def perform_health_check():
    """Effectue un health check complet avec option de démarrage automatique"""
    try:
        auto_start = request.json.get('auto_start', True) if request.json else True
        all_healthy, messages = health_check_service.perform_startup_checks(auto_start=auto_start)
        
        return jsonify({
            'success': True,
            'all_healthy': all_healthy,
            'messages': messages
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/health')
def simple_health():
    """Simple endpoint de santé pour OmniParser"""
    return jsonify({"status": "healthy", "service": "monopoly-ia"})

def run_app():
    """Démarre l'application Flask"""
    # Créer le dossier de contexte s'il n'existe pas
    os.makedirs(config.CONTEXT_DIR, exist_ok=True)
    os.makedirs(config.CONTEXT_HISTORY_DIR, exist_ok=True)
    
    # Démarrer le thread de capture du terminal
    terminal_thread = threading.Thread(target=capture_terminal_output, daemon=True)
    terminal_thread.start()
    
    # Démarrer le thread de vérification du statut de Dolphin
    dolphin_check_thread = threading.Thread(target=check_dolphin_status, daemon=True)
    dolphin_check_thread.start()
    
    # Démarrer l'application Flask
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG, use_reloader=False)

if __name__ == "__main__":
    run_app() 