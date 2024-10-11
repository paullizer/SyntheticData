from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, Markup, flash, send_file
from flask_session import Session 
import openai
import os
import requests
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from azure.cosmos import CosmosClient, PartitionKey, exceptions
import uuid
import datetime
import msal
from msal import ConfidentialClientApplication
import csv
from io import StringIO, BytesIO

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_KEY")
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB limit
app.config['VERSION'] = '0.3'

# Initialize Cosmos client
cosmos_endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")
cosmos_key = os.getenv("AZURE_COSMOS_KEY")
cosmos_db_name = os.getenv("AZURE_COSMOS_DB_NAME")
cosmos_data_container_name = os.getenv("AZURE_COSMOS_DATA_CONTAINER_NAME")
cosmos_files_container_name = os.getenv("AZURE_COSMOS_FILES_CONTAINER_NAME")

cosmos_client = CosmosClient(cosmos_endpoint, cosmos_key)
cosmos_db = cosmos_client.create_database_if_not_exists(id=cosmos_db_name)
cosmos_results_container = cosmos_db.create_container_if_not_exists(
    id=cosmos_data_container_name,
    partition_key=PartitionKey(path="/id"),
    offer_throughput=400
)
cosmos_files_container = cosmos_db.create_container_if_not_exists(
    id=cosmos_files_container_name,
    partition_key=PartitionKey(path="/id"),
    offer_throughput=400
)

# Configure Azure OpenAI
openai.api_type = os.getenv("AZURE_OPENAI_API_TYPE")  # e.g., "azure"
openai.api_base = os.getenv("AZURE_OPENAI_ENDPOINT")   # e.g., "https://your-resource-name.openai.azure.com/"
openai.api_version = os.getenv("AZURE_OPENAI_API_VERSION")  # e.g., "2023-05-15"
openai.api_key = os.getenv("AZURE_OPENAI_KEY")
MODEL = os.getenv("AZURE_OPENAI_MODEL")

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("MICROSOFT_PROVIDER_AUTHENTICATION_SECRET")
TENANT_ID = os.getenv("TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["User.Read"]  # Adjust scope according to your needs

# Allowed file extensions
ALLOWED_EXTENSIONS = {'txt', 'sql', 'csv'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return render_template('upload_content.html', error="File is too large. Maximum allowed size is 16MB."), 413

# Save results to Cosmos DB
def save_results_to_cosmos(results, data_model, body):
    # Get user information from session
    user_info = session.get("user", {})
    user_id = user_info.get("oid", "anonymous")  # Default to 'anonymous' if user ID not found
    user_email = user_info.get("email", "no-email")  # Default to 'no-email' if email not found
    
    result_data = {
        'id': str(uuid.uuid4()),
        'user_id': user_id,  # Save user ID with the result
        'user_email': user_email,  # Save user email with the result
        'data_model': data_model,
        'body': body,
        'results': results,
        'timestamp': str(datetime.datetime.utcnow())
    }
    
    try:
        cosmos_results_container.create_item(body=result_data)
    except exceptions.CosmosHttpResponseError as e:
        print(f"Error saving to Cosmos DB: {e}")

    return result_data['id']  # Return the result_id

@app.context_processor
def inject_previous_results():
    # Query Cosmos DB for previous results
    query = "SELECT c.id, c.data_model, c.timestamp FROM c"
    previous_results = []
    try:
        previous_results = list(cosmos_results_container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
    except Exception as e:
        print(f"Error fetching previous results: {e}")
    
    # Inject `previous_results` into the template context for all pages
    return {'previous_results': previous_results}

@app.route('/select_file', methods=['GET', 'POST'])
def select_file():
    if request.method == 'POST':
        selected_file_id = request.form.get('selected_file')
        if selected_file_id:
            try:
                # Fetch the file content from Cosmos DB
                file_item = cosmos_files_container.read_item(item=selected_file_id, partition_key=selected_file_id)

                session['data_model'] = file_item['content']
                return redirect(url_for('options'))
            except Exception as e:
                print(f"Error retrieving file: {e}")
                flash("An error occurred while retrieving the file.", "danger")
                return redirect(request.url)
    
    # Get sorting and search parameters from the request
    sort_by = request.args.get('sort_by', 'timestamp')  # Default to sorting by timestamp
    sort_order = request.args.get('sort_order', 'desc')  # Default to descending order
    search_query = request.args.get('search', '').lower()  # Default to empty search query
    
    # Get user_id from session
    user_id = session.get("user", {}).get("oid", "anonymous")

    # Query Cosmos DB for uploaded files
    files_query = f"SELECT c.id, c.filename, c.timestamp FROM c WHERE c.user_id = '{user_id}'"
    files = list(cosmos_files_container.query_items(query=files_query, enable_cross_partition_query=True))

    # Filter results based on the search query
    if search_query:
        files = [file for file in files if search_query in file['filename'].lower()]

    # Sort results based on sort_by and sort_order parameters
    if sort_by == 'filename':
        files.sort(key=lambda x: x['filename'].lower(), reverse=(sort_order == 'desc'))
    else:  # Default to sorting by timestamp
        files.sort(key=lambda x: x['timestamp'], reverse=(sort_order == 'desc'))

    return render_template('select_file.html', files=files, sort_by=sort_by, sort_order=sort_order, search_query=search_query)

@app.route("/login")
def login():
    # Create a MSAL application object
    msal_app = ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET,
    )
    # Get the login URL to redirect the user
    result = msal_app.get_authorization_request_url(SCOPE, redirect_uri=url_for("authorized", _external=True, _scheme='https'))
    print("Debug - MSAL auth URL and state:", result)
    return redirect(result)

@app.route("/getAToken")  # Redirect URI
def authorized():
    # Create a MSAL application object
    msal_app = ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET,
    )
    # Extract the code from the response
    code = request.args.get('code', '')
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,  # Misspelled in MSAL as scope
        redirect_uri=url_for("authorized", _external=True, _scheme='https')
    )
    if "error" in result:
        return f"Login failure: {result.get('error_description', result.get('error'))}"
    session["user"] = result.get("id_token_claims")
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()  # Clear the user session
    flash('You have been successfully logged out.', 'success')  # Optional: Notify the user of logout
    return redirect(url_for('index'))  # Redirect to the homepage or login page

@app.route('/', methods=['GET', 'POST'])
def index():
    # Clear specific keys instead of the whole session
    keys_to_clear = ['data_model', 'number_of_rows', 'preview', 'output_format']
    for key in keys_to_clear:
        session.pop(key, None)
    if request.method == 'POST':
        return redirect(url_for('content_method'))
    return render_template('index.html')

# Favicon route
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/history')
def history():
    # Get sorting and search parameters from the request
    sort_by = request.args.get('sort_by', 'timestamp')  # Default to sorting by timestamp
    sort_order = request.args.get('sort_order', 'desc')  # Default to descending order
    search_query = request.args.get('search', '').lower()  # Default to empty search query
    
    # Get user_id from session
    user_id = session.get("user", {}).get("oid", "anonymous")
    
    # Query Cosmos DB for user's saved results
    # Query Cosmos DB for user's saved results
    query = f"SELECT c.id, c.data_model, c.timestamp FROM c WHERE c.user_id = '{user_id}'"
    results = list(cosmos_results_container.query_items(query=query, enable_cross_partition_query=True))

    # Filter results based on the search query
    if search_query:
        results = [result for result in results if search_query in result['data_model'].lower()]

    # Sort results based on sort_by and sort_order parameters
    if sort_by == 'data_model':
        results.sort(key=lambda x: x['data_model'].lower(), reverse=(sort_order == 'desc'))
    else:  # Default to sorting by timestamp
        results.sort(key=lambda x: x['timestamp'], reverse=(sort_order == 'desc'))

    return render_template('history.html', results=results, sort_by=sort_by, sort_order=sort_order, search_query=search_query)

@app.route('/result/<result_id>')
def view_result(result_id):
    try:
        # Fetch the result from Cosmos DB
        result = cosmos_results_container.read_item(item=result_id, partition_key=result_id)
        print(f"Full Result: {result}")  # Log the entire result object

        # Render the template
        return render_template('view_result.html', result=result)
    except Exception as e:
        print(f"Error retrieving result: {e}")
        flash("Error retrieving result.", "danger")
        return redirect(url_for('history'))


@app.route('/content_method', methods=['GET', 'POST'])
def content_method():
    if request.method == 'POST':
        content_option = request.form.get('content_option')
        if content_option == 'provide_text':
            return redirect(url_for('provide_text'))
        elif content_option == 'upload_content':
            return redirect(url_for('upload_content'))
        elif content_option == 'select_file':
            return redirect(url_for('select_file'))
    return render_template('content_method.html')

@app.route('/provide_text', methods=['GET', 'POST'])
def provide_text():
    if request.method == 'POST':
        data_model = request.form.get('data_model')
        session['data_model'] = data_model
        return redirect(url_for('options'))
    return render_template('provide_text.html')

@app.route('/upload_content', methods=['GET', 'POST'])
def upload_content():
    if request.method == 'POST':
        file = request.files.get('file')
        if file and allowed_file(file.filename):
            try:
                filename = secure_filename(file.filename)
                file_extension = filename.rsplit('.', 1)[1].lower()
                
                # Read file content
                content = file.read().decode('utf-8', errors='ignore')
                
                if not content.strip():
                    flash("The uploaded file is empty or could not be read.", "danger")
                    return redirect(request.url)
                
                # Get user information from session
                user_info = session.get("user", {})
                user_id = user_info.get("oid", "anonymous")  # Default to 'anonymous' if user ID not found
                user_email = user_info.get("email", "no-email")  # Default to 'no-email' if email not found

                # Save the file data to Cosmos DB
                file_data = {
                    'id': str(uuid.uuid4()),
                    'content': content,
                    'filename': filename,
                    'file_extension': file_extension,
                    'timestamp': str(datetime.datetime.utcnow()),
                    'user_id': user_id,  # Associate with user
                    'user_email': user_email  # Save user email with the result
                }
                cosmos_files_container.create_item(body=file_data)

                session['data_model'] = content
                
                flash("File uploaded successfully!", "success")
                return redirect(url_for('options'))
            except Exception as e:
                print(f"Error processing the uploaded file: {e}")
                flash("An error occurred while processing the uploaded file.", "danger")
                return redirect(request.url)
        else:
            flash("Invalid file type. Please upload a .sql, .csv, or .txt file.", "danger")
            return redirect(request.url)
    
    return render_template('upload_content.html')

@app.route('/options', methods=['GET', 'POST'])
def options():
    if request.method == 'POST':
        number_of_rows = request.form.get('number_of_rows')
        try:
            number_of_rows = int(number_of_rows)
        except ValueError:
            number_of_rows = 100  # default value

        preview = request.form.get('preview') == 'on'
        output_format = request.form.get('output_format', 'csv')
        
        session['number_of_rows'] = number_of_rows
        session['preview'] = preview
        session['output_format'] = output_format
        
        return redirect(url_for('results'))
    return render_template('options.html')

@app.route('/results')
def results():
    data_model = session.get('data_model', '')
    number_of_rows = session.get('number_of_rows', 100)
    preview = session.get('preview', True)
    output_format = session.get('output_format', 'csv')
    results = {}
    
    if not data_model:
        flash("No data model provided.", "danger")
        return redirect(url_for('index'))
    
    # Use OpenAI API to generate synthetic data
    system_prompt = "You are an AI assistant that generates synthetic data based on a given data model."
    user_prompt = f"Generate {number_of_rows} rows of synthetic data based on the following data model:\n{data_model}\nOutput the data in {output_format.upper()} format."

    try:
        response = openai.ChatCompletion.create(
            engine=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=2048,
            temperature=0.7
        )
        
        generated_data = response['choices'][0]['message']['content'].strip()
        
        # If preview is true, extract the first 10 rows
        if preview:
            # Depending on the output format, parse the data accordingly
            if output_format.lower() == 'csv':
                data_lines = generated_data.splitlines()
                preview_data = '\n'.join(data_lines[:11])  # Include header and first 10 rows
                # Convert preview_data to HTML table
                reader = csv.reader(StringIO(preview_data))
                table = [row for row in reader]
                results['table'] = table
            else:
                # Handle other formats accordingly
                preview_data = generated_data[:1000]  # Show first 1000 chars
                results['preview_data'] = preview_data
        else:
            results['preview_data'] = ''
        
        results['generated_data'] = generated_data
        
        # Save results to Cosmos DB and capture the result_id
        result_id = save_results_to_cosmos(results, data_model, '')
        
        # Pass result_id to the template
        return render_template(
            'results.html',
            results=results,
            data_model=data_model,
            number_of_rows=number_of_rows,
            preview=preview,
            output_format=output_format,
            result_id=result_id  # Pass the result_id here
        )
    
    except Exception as e:
        print(f"Error generating synthetic data: {e}")
        flash("An error occurred while generating synthetic data.", "danger")
        return redirect(url_for('index'))


@app.route('/download/<result_id>')
def download_data(result_id):
    try:
        # Fetch the result from Cosmos DB
        result = cosmos_results_container.read_item(item=result_id, partition_key=result_id)
        generated_data = result.get('results', {}).get('generated_data', '')
        output_format = result.get('output_format', 'csv')
        
        if not generated_data:
            flash("No data available for download.", "danger")
            return redirect(url_for('results'))
        
        # Create a BytesIO object
        data_io = BytesIO()
        data_io.write(generated_data.encode('utf-8'))
        data_io.seek(0)
        
        # Set the appropriate MIME type
        if output_format == 'csv':
            mimetype = 'text/csv'
            extension = 'csv'
        elif output_format == 'sql':
            mimetype = 'application/sql'
            extension = 'sql'
        else:
            mimetype = 'text/plain'
            extension = 'txt'
        
        return send_file(
            data_io,
            mimetype=mimetype,
            as_attachment=True,
            download_name=f'synthetic_data.{extension}'  # Use 'download_name' instead of 'attachment_filename'
        )
    except Exception as e:
        print(f"Error during data download: {e}")
        flash("An error occurred while downloading the data.", "danger")
        return redirect(url_for('results'))

    
if __name__ == '__main__':
    app.run(debug=True)
