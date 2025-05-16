// Check what might be causing the null reference error in main.js
// This is a safer version that checks for element existence before adding listeners

document.addEventListener('DOMContentLoaded', function() {
    // Initialize elements with null-checking
    const checkRankButton = document.getElementById('checkRankButton');
    const resultModal = document.getElementById('resultModal') ? 
        new bootstrap.Modal(document.getElementById('resultModal')) : null;
    
    // Only attach event handlers if elements exist
    if (checkRankButton) {
        checkRankButton.addEventListener('click', function() {
            const rocketId = document.getElementById('rocketId')?.value.trim() || '';
            const platform = document.getElementById('platform')?.value || '';
            const discordId = document.getElementById('discordId')?.value.trim() || '';
            
            if (!rocketId) {
                alert('Please enter your Rocket League ID');
                return;
            }
            
            // Show loading state if verificationResult exists
            const resultDiv = document.getElementById('verificationResult');
            if (resultDiv) {
                resultDiv.innerHTML = `
                    <div class="text-center py-3">
                        <div class="spinner-border text-primary" role="status">
                            <span class="visually-hidden">Loading...</span>
                        </div>
                        <p class="mt-2">Checking your rank...</p>
                    </div>
                `;
            }
            
            // Disable the button while checking
            checkRankButton.disabled = true;
            checkRankButton.innerHTML = `
                <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
                Checking...
            `;
            
            // Call our API endpoint to get the rank data
            fetchRankData(rocketId, platform, discordId);
        });
    } else {
        console.error('Check Rank button not found in the DOM');
    }
    
    // Initialize rank selector if it exists
    const rankSelect = document.getElementById('rlRank');
    if (rankSelect) {
        setupRankSelector();
        
        // Check for a reset
        checkResetStatus();
        
        // Fallback check for very old verifications
        checkLastVerificationAge();
    }
    
    function fetchRankData(username, platform, discordId) {
        // Build the API URL with query parameters
        let apiUrl = `/api/rank-check?platform=${encodeURIComponent(platform)}&username=${encodeURIComponent(username)}`;
        
        if (discordId) {
            apiUrl += `&discord_id=${encodeURIComponent(discordId)}`;
        }
        
        // Make the API request
        fetch(apiUrl)
            .then(response => response.json())
            .then(data => {
                // Process the API response
                if (data.success) {
                    displayRankResults(data, discordId);
                } else {
                    showError(data.message || "Could not retrieve rank information. Please check your ID and platform.");
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showError("An error occurred while checking your rank. Please try again later.");
            })
            .finally(() => {
                // Re-enable button regardless of outcome
                if (checkRankButton) {
                    checkRankButton.disabled = false;
                    checkRankButton.innerHTML = `<i class="fas fa-check-circle me-2"></i> Check My Rank`;
                }
            });
    }
    
    function displayRankResults(data, discordId) {
        const resultDiv = document.getElementById('verificationResult');
        if (!resultDiv) return;
    
        // Use the stored rankValue or fall back to a tier-based value
        const rankValue = data.rankValue || data.tier?.toLowerCase().replace(' ', '_') || 'rank_c';
    
        // Determine the rank icon based on tier
        let rankIcon = 'fas fa-medal';
        if (data.tier === 'Rank A') {
            rankIcon = 'fas fa-trophy';
        } else if (data.tier === 'Rank B') {
            rankIcon = 'fas fa-award';
        }
    
        // Determine the rank color class
        const rankColorClass = data.tier === 'Rank A' ? 'rank-a' : data.tier === 'Rank B' ? 'rank-b' : 'rank-c';
        const badgeClass = data.tier === 'Rank A' ? 'bg-danger' : data.tier === 'Rank B' ? 'bg-primary' : 'bg-success';
    
        // Create a success notification at the top
        const successAlert = `
            <div class="alert alert-success mb-3">
                <i class="fas fa-check-circle me-2"></i> Rank verification successful!
            </div>
        `;
    
        // Create a discord notification at the bottom
        const discordAlert = data.role_assignment && data.role_assignment.success ?
            `<div class="alert alert-success mt-3">
                <i class="fab fa-discord me-2"></i> Discord role assigned successfully!
            </div>` :
            `<div class="alert alert-warning mt-3">
                <i class="fab fa-discord me-2"></i> Could not assign Discord role automatically. Please contact an admin.
            </div>`;
    
        // Use the rank card design
        resultDiv.innerHTML = `
            ${successAlert}
    
            <div class="rank-card">
                <div class="rank-header ${rankColorClass}">
                    <i class="${rankIcon} fa-2x"></i>
                </div>
                <div class="rank-body">
                    <h5 class="rank-title">${data.tier}</h5>
                    <p class="rank-description">${data.rank}</p>
                    <p class="rank-mmr">Starting MMR: ${data.mmr}</p>
                    <span class="badge ${badgeClass}">${data.tier} Role</span>
                </div>
            </div>
    
            ${discordAlert}
        `;
        
        // Show in the modal too if it exists
        const modalContent = document.getElementById('resultModalContent');
        if (modalContent && resultModal) {
            let modalContent = `
                <div class="text-center">
                    <div class="mb-3">
                        <i class="fas fa-check-circle fa-4x text-success"></i>
                    </div>
                    <h4>Your rank has been verified!</h4>
                    <p class="mb-3">Based on your ${data.rank} rank.</p>
                    
                    <div class="alert alert-info">
                        <strong>Discord Role:</strong> ${data.tier}<br>
                        <strong>Starting MMR:</strong> ${data.mmr}
                    </div>
            `;
            
            // Add role assignment result
            if (data.role_assignment && data.role_assignment.success) {
                modalContent += `
                    <div class="alert alert-success">
                        <i class="fab fa-discord me-2"></i> Your Discord role has been updated automatically!
                    </div>
                `;
            } else {
                modalContent += `
                    <div class="alert alert-warning">
                        <i class="fas fa-exclamation-triangle me-2"></i> Could not update your Discord role automatically. Please contact an admin.
                    </div>
                `;
            }
            
            modalContent += `
                    <p>You can now join the 6 Mans queue in our Discord server.</p>
                    <a href="https://discord.gg/your-discord" target="_blank" class="btn btn-primary mt-2">
                        <i class="fab fa-discord me-2"></i> Join Our Discord
                    </a>
                </div>
            `;
            
            document.getElementById('resultModalContent').innerHTML = modalContent;
            
            // Show modal
            resultModal.show();
        }
    }
    
    function showError(message) {
        // Update verification result section
        const resultDiv = document.getElementById('verificationResult');
        if (resultDiv) {
            resultDiv.innerHTML = `
                <div class="alert alert-danger mb-0">
                    <i class="fas fa-exclamation-circle me-2"></i> ${message}
                </div>
            `;
        }
        
        // Update modal if it exists
        const modalContent = document.getElementById('resultModalContent');
        if (modalContent && resultModal) {
            document.getElementById('resultModalContent').innerHTML = `
                <div class="text-center">
                    <div class="mb-3">
                        <i class="fas fa-exclamation-circle fa-4x text-danger"></i>
                    </div>
                    <h4>Verification Failed</h4>
                    <div class="alert alert-danger mb-0">
                        ${message}
                    </div>
                    <p class="mt-3">Please check your Rocket League ID and platform, then try again.</p>
                    <p class="mt-3">If the problem persists, you can still join our Discord and request manual verification from an admin.</p>
                    <a href="https://discord.gg/your-discord" target="_blank" class="btn btn-primary mt-2">
                        <i class="fab fa-discord me-2"></i> Join Our Discord
                    </a>
                </div>
            `;
            
            // Show modal
            resultModal.show();
        }
    }
});

// Other functions referenced earlier should be defined outside the DOMContentLoaded event
function setupRankSelector() {
    console.log("Setting up rank selector...");
    const rankSelect = document.getElementById('rlRank');
    const verifyButton = document.getElementById('verifyRankButton');
    const rankPreview = document.getElementById('rankPreview');
    
    if (!rankSelect || !verifyButton) {
        console.error("Required elements for rank selector not found");
        return;
    }
    
    // Show rank image when selection changes
    rankSelect.addEventListener('change', function() {
        const selectedOption = this.options[this.selectedIndex];
        const value = this.value;
        const text = selectedOption.text;
        const imagePath = selectedOption.getAttribute('data-icon');

        if (rankPreview) {
            const rankImage = document.getElementById('rankImage');
            const rankName = document.getElementById('rankName');
            
            if (rankImage && rankName) {
                rankImage.src = imagePath;
                rankImage.alt = text;
                rankName.textContent = text;
                rankPreview.classList.remove('d-none');
            }
        }
    });
}

function checkResetStatus() {
    console.log("Checking leaderboard reset status...");
    // Implementation details...
}

function checkLastVerificationAge() {
    console.log("Checking verification age...");
    // Implementation details...
}